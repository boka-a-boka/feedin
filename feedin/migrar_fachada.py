import sqlite3
import os


def migrar_banco_sqlite(caminho_banco):
    print(f"🔄 Iniciando processo de migração no banco: {caminho_banco}")

    if not os.path.exists(caminho_banco):
        print(f"❌ Erro: O arquivo de banco de dados '{caminho_banco}' não foi encontrado.")
        return

    # 1. Conecta ao banco de dados
    conn = sqlite3.connect(caminho_banco)
    cursor = conn.cursor()

    try:
        # Desativa chaves estrangeiras temporariamente para evitar travas no DROP/RENAME
        cursor.execute("PRAGMA foreign_keys = OFF;")

        print("📦 Criando nova estrutura temporária (ese_empresa_nova)...")
        # 2. Cria a nova tabela com a modelagem exata (já incluindo a coluna 'fachada')
        cursor.execute("""
            CREATE TABLE ese_empresa_nova (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                proprietario_id INTEGER NOT NULL,
                local_id INTEGER,
                nome VARCHAR(100) NOT NULL,
                categoria VARCHAR(50),
                slug VARCHAR(100) UNIQUE,
                logomarca VARCHAR(255),
                fachada VARCHAR(255), -- 🌟 A nova coluna vital inserida aqui
                documento_oficial VARCHAR(14) UNIQUE,
                tipo_documento VARCHAR(4),
                data_fundacao DATE,
                dominio_web VARCHAR(255) UNIQUE,
                historia_ocupacao TEXT,
                missao_valores TEXT,
                termo_responsabilidade_aceito BOOLEAN NOT NULL DEFAULT 0,
                data_aceite_termo DATETIME,
                ip_aceite_termo VARCHAR(45),
                status_homologacao VARCHAR(30) DEFAULT 'aguardando_dados',
                cor_primaria VARCHAR(7) DEFAULT '#111827',
                cor_secundaria VARCHAR(7) DEFAULT '#6B7280',
                FOREIGN KEY(proprietario_id) REFERENCES usuario(id),
                FOREIGN KEY(local_id) REFERENCES locais(id)
            );
        """)

        print("📥 Clonando dados da tabela antiga para a nova...")
        # 3. Mapeia e injeta os dados existentes da tabela antiga para a nova.
        # Note que pulamos a coluna 'fachada' na leitura, deixando ela receber NULL por padrão nos registros antigos.
        cursor.execute("""
            INSERT INTO ese_empresa_nova (
                id, proprietario_id, local_id, nome, categoria, slug, logomarca,
                documento_oficial, tipo_documento, data_fundacao, dominio_web,
                historia_ocupacao, missao_valores, termo_responsabilidade_aceito,
                data_aceite_termo, ip_aceite_termo, status_homologacao,
                cor_primaria, cor_secundaria
            )
            SELECT 
                id, proprietario_id, local_id, nome, categoria, slug, logomarca,
                documento_oficial, tipo_documento, data_fundacao, dominio_web,
                historia_ocupacao, missao_valores, termo_responsabilidade_aceito,
                data_aceite_termo, ip_aceite_termo, status_homologacao,
                cor_primaria, cor_secundaria
            FROM ese_empresa;
        """)

        print("🗑️ Deletando a tabela antiga (ese_empresa)...")
        # 4. Remove a tabela desatualizada
        cursor.execute("DROP TABLE ese_empresa;")

        print("🏷️ Renomeando 'ese_empresa_nova' para 'ese_empresa'...")
        # 5. Restabelece o nome oficial da tabela com a nova estrutura ativa
        cursor.execute("ALTER TABLE ese_empresa_nova RENAME TO ese_empresa;")

        # Reativa a checagem de chaves estrangeiras
        cursor.execute("PRAGMA foreign_keys = ON;")

        # Grava as alterações permanentemente
        conn.commit()
        print("🎉 Migração concluída com sucesso! Coluna 'fachada' integrada e dados preservados.")

    except sqlite3.Error as erro:
        # Se algo der errado no meio do caminho, desfaz tudo para não corromper o banco
        conn.rollback()
        print(f"🚨 Erro crítico durante a migração. Operação abortada. Detalhes: {erro}")

    finally:
        conn.close()


if __name__ == "__main__":
    NOME_DO_BANCO = r"C:\Users\Estava-La01\PycharmProjects\ProjetoFeedIn\instance\feedin-db.db"
    migrar_banco_sqlite(NOME_DO_BANCO)

    migrar_banco_sqlite(NOME_DO_BANCO)