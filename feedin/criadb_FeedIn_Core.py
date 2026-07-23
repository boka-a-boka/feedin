# C:\Users\Estava-La01\PycharmProjects\ProjetoFeedIn\feedin\atualiza_tabela_cliente.py
import sqlite3
import os

# 1. Alvo exato no seu banco oficial
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
PROJETO_DIR = os.path.dirname(BASE_DIR)
DB_PATH = os.path.join(PROJETO_DIR, 'instance', 'feedin-db.db')

print(f"🎯 CONECTANDO AO BANCO OFICIAL: {DB_PATH}")

conexao = sqlite3.connect(DB_PATH)
cursor = conexao.cursor()

# 2. Comando para limpar a tabela desalinhada antiga
script_drop = "DROP TABLE IF EXISTS mod_cadastro_cliente;"

# 3. Script SQL estruturado exatamente igual ao seu db.Model (ModCadastroCliente) com UUID4
# 🌟 ALTERAÇÃO: O ID agora é VARCHAR(36) PRIMARY KEY sem AUTOINCREMENT numérico
script_create_cliente = """
CREATE TABLE mod_cadastro_cliente (
    id VARCHAR(36) PRIMARY KEY,
    usuario_id INTEGER UNIQUE,
    nome TEXT NOT NULL,
    email TEXT UNIQUE,
    whatsapp TEXT NOT NULL,
    data_nascimento DATE,
    cpf_hash TEXT UNIQUE NOT NULL,
    cpf_encrypted BLOB NOT NULL,
    username_modulo TEXT UNIQUE NOT NULL,
    senha_hash TEXT NOT NULL,
    status_conta TEXT DEFAULT 'ativo',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (usuario_id) REFERENCES usuario (id)
);
"""

# 4. Criação manual dos índices para manter a performance idêntica ao SQLAlchemy
script_indices = """
CREATE INDEX IF NOT EXISTS ix_mod_cadastro_cliente_email ON mod_cadastro_cliente (email);
CREATE INDEX IF NOT EXISTS ix_mod_cadastro_cliente_cpf_hash ON mod_cadastro_cliente (cpf_hash);
"""

try:
    print("🔄 Removendo tabela antiga 'mod_cadastro_cliente' (se houver)...")
    cursor.execute(script_drop)

    print("🔄 Injetando nova tabela 'mod_cadastro_cliente' unificada com padrão UUID...")
    cursor.execute(script_create_cliente)

    print("⚡ Criando índices de performance (email e cpf_hash)...")
    cursor.executescript(script_indices)

    # Grava as alterações no disco de forma efetiva
    conexao.commit()
    print("💾 Alterações persistidas com sucesso no arquivo oficial!")

    # Validação no catálogo do SQLite
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = 'mod_cadastro_cliente';")
    tabela_criada = cursor.fetchone()

    # 🔑 Validação das colunas físicas
    if tabela_criada:
        print(f"📊 Confirmação do Banco: Tabela '{tabela_criada[0]}' está ATIVA e alinhada!")

        # Exibe as colunas criadas para sua total segurança antes de rodar o app
        cursor.execute("PRAGMA table_info(mod_cadastro_cliente);")
        colunas = cursor.fetchall()
        print("📋 Estrutura física atualizada no banco:")
        for col in colunas:
            print(f"   -> Coluna: {col[1]} ({col[2]})")
    else:
        print("⚠️ Atenção: A tabela não foi localizada no catálogo após a execução.")

except Exception as e:
    print(f"💥 Erro na execução do SQL: {e}")

finally:
    conexao.close()
    print("🔌 Conexão encerrada com segurança.")