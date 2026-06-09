# C:\Users\Estava-La01\PycharmProjects\ProjetoFeedIn\feedin\modules\agenda\init_compliance.py
import sqlite3
import os

# 1. MAPEIA O CAMINHO DO SEU BANCO DE DADOS
BASE_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
DB_PATH = os.path.join(BASE_DIR, "instance", "feedin-db.db")

print(f"🔍 Procurando banco em: {DB_PATH}")

if not os.path.exists(DB_PATH):
    print("❌ Erro: O arquivo feedin-db.db não foi localizado.")
    exit()

conexao = sqlite3.connect(DB_PATH)
cursor = conexao.cursor()

# 2. SCRIPT SINTÁTICO DA TABELA DE LIMBO
script_tabela_fila = """
CREATE TABLE IF NOT EXISTS mod_fila_ativacao_cliente (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario_id INTEGER,
    nome TEXT NOT NULL,
    cpf TEXT NOT NULL UNIQUE,
    whatsapp TEXT NOT NULL,
    email TEXT,
    data_disparo DATETIME DEFAULT CURRENT_TIMESTAMP,
    data_expiracao DATETIME NOT NULL,
    data_tentativa_abertura DATETIME,
    FOREIGN KEY (usuario_id) REFERENCES usuario (id)
);
"""

# 3. SCRIPT SINTÁTICO DA NOVA TABELA mod_cadastro_cliente (Ajustada para o Hash)
script_tabela_cadastro = """
CREATE TABLE IF NOT EXISTS mod_cadastro_cliente (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario_id INTEGER UNIQUE,
    nome TEXT NOT NULL,
    cpf_hash VARCHAR(64) NOT NULL UNIQUE,
    cpf_encrypted BLOB NOT NULL,
    whatsapp TEXT NOT NULL,
    email TEXT,
    data_nascimento DATE NOT NULL,
    username_modulo TEXT NOT NULL UNIQUE,
    senha_modulo_hash TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (usuario_id) REFERENCES usuario (id)
);
"""

try:
    print("🛡️ Injetando tabelas de segurança...")

    # Executa a criação da fila
    cursor.execute(script_tabela_fila)

    # Executa a criação da tabela blindada
    cursor.execute(script_tabela_cadastro)

    # Criação do índice para performance no hash
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_cpf_hash ON mod_cadastro_cliente(cpf_hash);")

    conexao.commit()
    print("💾 Tabelas persistidas fisicamente com sucesso no SQLite!")
except Exception as e:
    print(f"💥 Erro ao executar o SQL: {e}")
finally:
    conexao.close()