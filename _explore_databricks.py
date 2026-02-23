"""
Comparação Delta KM: Databricks vs Excel
==========================================
1. Tentar query no bronze (historico veiculo cliente)
2. Calcular MAX-MIN no gold (fact_maintenanceservices)
3. Comparar com Excel
"""
import openpyxl
from databricks import sql
import os
from collections import defaultdict

EXCEL_PATH = r"4 -  KM Rodado_Geral 1.xlsx"

conn = sql.connect(
    server_hostname='adb-7941093640821140.0.azuredatabricks.net',
    http_path='/sql/1.0/warehouses/ce56ec5f5d0a3e07',
    access_token=os.environ.get('DATABRICKS_TOKEN')
)
cursor = conn.cursor()

# ═══════════════════════════════════════════
# TESTE 1: Query no bronze
# ═══════════════════════════════════════════
print("=" * 70)
print("  TESTE 1: Query no schema bronze")
print("=" * 70)
try:
    cursor.execute("""
        SELECT *
        FROM hive_metastore.bronze.bckof19st_manutencao_veiculo_cliente_historico vch
        LEFT JOIN hive_metastore.bronze.bckof19st_manutencao_veiculo_cliente vc
          ON vc.CD_VEICULO_CLIENTE = vch.CD_VEICULO_CLIENTE
        WHERE vc.ds_placa = 'FNZ2E46'
        LIMIT 5
    """)
    cols = [d[0] for d in cursor.description]
    rows = cursor.fetchall()
    print(f"  ✅ SUCESSO! Colunas: {cols}")
    print(f"  Registros: {len(rows)}")
    for row in rows:
        print(f"  {dict(zip(cols, row))}")
except Exception as e:
    print(f"  ❌ ERRO: {e}")

# ═══════════════════════════════════════════
# TESTE 2: Delta KM no gold (MAX-MIN por placa/mês)
# ═══════════════════════════════════════════
print("\n" + "=" * 70)
print("  TESTE 2: Delta KM no gold (MAX-MIN MileageNumber)")
print("=" * 70)

# Pegar top 10 placas do Excel
wb = openpyxl.load_workbook(EXCEL_PATH, read_only=True, data_only=True)
ws = wb["KM rodado"]
excel_data = defaultdict(lambda: defaultdict(float))
for row in ws.iter_rows(min_row=2, values_only=True):
    placa, km, data = row[0], row[8], row[11]
    if placa and km and data and km > 0:
        try:
            excel_data[placa][(data.month, data.year)] += km
        except:
            pass
wb.close()

top_placas = sorted(excel_data.keys(), key=lambda p: sum(excel_data[p].values()), reverse=True)[:10]
top_str = "','".join(top_placas)

# Calcular Delta KM = MAX(MileageNumber) - MIN(MileageNumber) por placa/mês
cursor.execute(f"""
    SELECT 
        v.LicensePlate as placa,
        MONTH(s.ReferenceDate) as mes,
        YEAR(s.ReferenceDate) as ano,
        MAX(s.MileageNumber) as max_km,
        MIN(s.MileageNumber) as min_km,
        MAX(s.MileageNumber) - MIN(s.MileageNumber) as delta_km,
        COUNT(*) as num_os
    FROM hive_metastore.gold.fact_maintenanceservices s
    JOIN hive_metastore.gold.dim_maintenancevehicles v
      ON v.Sk_MaintenanceVehicle = s.Sk_MaintenanceVehicle
    WHERE v.LicensePlate IN ('{top_str}')
      AND s.ReferenceDate >= '2025-01-01'
      AND s.ReferenceDate < '2026-02-01'
      AND s.MileageNumber > 0
    GROUP BY v.LicensePlate, YEAR(s.ReferenceDate), MONTH(s.ReferenceDate)
    ORDER BY v.LicensePlate, ano, mes
""")
cols = [d[0] for d in cursor.description]
db_delta = defaultdict(dict)
for row in cursor.fetchall():
    d = dict(zip(cols, row))
    db_delta[d['placa']][(d['mes'], d['ano'])] = d

# Comparar
print(f"\n  {'Placa':>10s} | {'Mês':>8s} | {'Excel KM':>10s} | {'DB Delta':>10s} | {'Diff%':>8s} | {'#OS':>4s} | Status")
print(f"  {'-'*10} | {'-'*8} | {'-'*10} | {'-'*10} | {'-'*8} | {'-'*4} | {'-'*15}")

erros = []
matches = 0
total = 0

for placa in top_placas:
    all_months = sorted(set(list(excel_data[placa].keys()) + list(db_delta.get(placa, {}).keys())))
    for (m, a) in all_months:
        excel_km = excel_data[placa].get((m, a), 0)
        db_info = db_delta.get(placa, {}).get((m, a), {})
        delta = db_info.get('delta_km', 0)
        num_os = db_info.get('num_os', 0)
        
        if excel_km > 0 and delta > 0:
            total += 1
            diff_pct = ((delta - excel_km) / excel_km) * 100
            status = "✅ OK" if abs(diff_pct) < 30 else "⚠️ DIFF"
            if abs(diff_pct) < 30:
                matches += 1
            else:
                erros.append((placa, f"{a}-{m:02d}", excel_km, delta, diff_pct))
            print(f"  {placa:>10s} | {a}-{m:02d} | {excel_km:>10,.0f} | {delta:>10,.0f} | {diff_pct:>+7.1f}% | {num_os:>4d} | {status}")
        elif excel_km > 0 and delta == 0:
            total += 1
            if num_os <= 1:
                print(f"  {placa:>10s} | {a}-{m:02d} | {excel_km:>10,.0f} | {'N/A':>10s} | {'N/A':>8s} | {num_os:>4d} | ⚠️ Poucas OS")
            else:
                print(f"  {placa:>10s} | {a}-{m:02d} | {excel_km:>10,.0f} | {delta:>10,.0f} | {'-100%':>8s} | {num_os:>4d} | ❌ Sem delta")

print(f"\n  === RESULTADO ===")
print(f"  Total comparações: {total}")
print(f"  Match (<30% diff): {matches} ({matches/max(total,1)*100:.0f}%)")
print(f"  Divergentes: {total - matches}")

# ═══════════════════════════════════════════
# TESTE 3: KM mensal agregado — Delta por frota inteira
# ═══════════════════════════════════════════
print("\n" + "=" * 70)
print("  TESTE 3: KM mensal TOTAL (Delta MAX-MIN por placa, somado)")
print("=" * 70)

# Excel mensal
excel_monthly = defaultdict(float)
for placa in excel_data:
    for (m, a), km in excel_data[placa].items():
        excel_monthly[(m, a)] += km

cursor.execute("""
    SELECT 
        sub.ano, sub.mes,
        SUM(sub.delta_km) as total_delta_km,
        COUNT(*) as num_placas
    FROM (
        SELECT 
            v.LicensePlate,
            YEAR(s.ReferenceDate) as ano,
            MONTH(s.ReferenceDate) as mes,
            MAX(s.MileageNumber) - MIN(s.MileageNumber) as delta_km,
            COUNT(*) as num_os
        FROM hive_metastore.gold.fact_maintenanceservices s
        JOIN hive_metastore.gold.dim_maintenancevehicles v
          ON v.Sk_MaintenanceVehicle = s.Sk_MaintenanceVehicle
        WHERE v.AdditionalInformation3Description IN 
          ('REGIONAL 1','REGIONAL 2','REGIONAL 3','REGIONAL 4',
           'REGIONAL 5','REGIONAL 6','REGIONAL 7','REGIONAL 8')
          AND s.ReferenceDate >= '2025-01-01'
          AND s.ReferenceDate < '2026-02-01'
          AND s.MileageNumber > 0
        GROUP BY v.LicensePlate, YEAR(s.ReferenceDate), MONTH(s.ReferenceDate)
        HAVING COUNT(*) >= 2
    ) sub
    WHERE sub.delta_km > 0
    GROUP BY sub.ano, sub.mes
    ORDER BY sub.ano, sub.mes
""")
cols = [d[0] for d in cursor.description]
print(f"\n  {'Mês':>8s} | {'Excel KM':>14s} | {'DB Delta KM':>14s} | {'Diff%':>8s} | {'Placas DB':>10s}")
print(f"  {'-'*8} | {'-'*14} | {'-'*14} | {'-'*8} | {'-'*10}")
for row in cursor.fetchall():
    d = dict(zip(cols, row))
    key = (d['mes'], d['ano'])
    excel_km = excel_monthly.get(key, 0)
    db_km = d['total_delta_km']
    diff = ((db_km - excel_km) / max(excel_km, 1)) * 100 if excel_km > 0 else 0
    print(f"  {d['ano']}-{d['mes']:02d} | {excel_km:>14,.0f} | {db_km:>14,.0f} | {diff:>+7.1f}% | {d['num_placas']:>10,d}")

cursor.close()
conn.close()
