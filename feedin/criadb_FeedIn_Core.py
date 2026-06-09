# C:\Users\Estava-La01\PycharmProjects\ProjetoFeedIn\feedin\criadb_FeedIn_Core.py
import sqlite3
import os

# 1. Alvo exato no seu banco oficial
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
PROJETO_DIR = os.path.dirname(BASE_DIR)
DB_PATH = os.path.join(PROJETO_DIR, 'instance', 'feedin-db.db')

print(f"🎯 CONECTANDO AO BANCO OFICIAL: {DB_PATH}")

conexao = sqlite3.connect(DB_PATH)
cursor = conexao.cursor()

# 2. Scripts SQL para criar as duas tabelas da estrutura profissional
script_cargos = """
CREATE TABLE IF NOT EXISTS cargos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nome_cargo TEXT NOT NULL UNIQUE
);
"""

script_contratos = """
CREATE TABLE IF NOT EXISTS colaborador_contratos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    id_usuario INTEGER NOT NULL,
    id_local INTEGER NOT NULL,
    id_cargo INTEGER NOT NULL,
    data_contratacao DATETIME NOT NULL,
    data_desligamento DATETIME,
    hora_inicio_expediente TEXT NOT NULL,
    hora_fim_expediente TEXT NOT NULL,
    status_profissional TEXT DEFAULT 'ativo',
    FOREIGN KEY (id_usuario) REFERENCES usuario (id),
    FOREIGN KEY (id_local) REFERENCES locais (id),
    FOREIGN KEY (id_cargo) REFERENCES cargos (id)
);
"""

try:
    print("🔄 Injetando tabela 'cargos'...")
    cursor.execute(script_cargos)

    print("🔄 Injetando tabela 'colaborador_contratos' com regras de expediente...")
    cursor.execute(script_contratos)

    # Grava as alterações no disco
    conexao.commit()
    print("💾 Alterações persistidas com sucesso no arquivo oficial!")

    # Validação no catálogo do SQLite
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('cargos', 'colaborador_contratos');")
    tabelas_criadas = cursor.fetchall()

    print(f"📊 Confirmação do Banco: Tabelas ativas -> {[t[0] for t in tabelas_criadas]}")

except Exception as e:
    print(f"💥 Erro na execução do SQL: {e}")

finally:
    conexao.close()
    print("🔌 Conexão encerrada com segurança.")