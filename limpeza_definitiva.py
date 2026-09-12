import sqlite3
import os

# Localiza o arquivo .db do SQLite
db_path = os.path.join(os.path.dirname(__file__), 'instance', 'feedin.db')

if not os.path.exists(db_path):
    # Fallback para o caminho padrão na raiz caso não esteja em /instance
    db_path = os.path.join(os.path.dirname(__file__), 'feedin.db')

print(f"Conectando diretamente ao banco: {db_path}")

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

try:
    print("\n--- INSPECIONANDO TIPOS EM ESE_SERVICO_OFERECIDO ---")
    cursor.execute("SELECT id, empresa_id, pontos_fidelidade, typeof(pontos_fidelidade) FROM ese_servico_oferecido;")
    rows = cursor.fetchall()

    for r in rows:
        print(f"ID: {r[0]} | Empresa: {r[1]} | Valor: {r[2]} | Tipo SQLite: {r[3]}")

    print("\n--- APLICANDO CAST NUMÉRICO FORÇADO ---")
    # Força a conversão do valor para REAL/NUMERIC no motor do SQLite
    cursor.execute("UPDATE ese_servico_oferecido SET pontos_fidelidade = CAST(1.00 AS REAL);")

    # Verifica se existe a mesma situação na tabela ese_servico_preco
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ese_servico_preco';")
    if cursor.fetchone():
        cursor.execute(
            "UPDATE ese_servico_preco SET novo_valor = CAST(0.00 AS REAL) WHERE novo_valor IS NULL OR typeof(novo_valor) = 'text';")

    conn.commit()
    print("\n✅ Registros atualizados com sucesso para valores REAL/Decimal válidos!")

except Exception as e:
    conn.rollback()
    print(f"\n❌ Erro durante a atualização: {e}")
finally:
    conn.close()