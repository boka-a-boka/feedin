from feedin import app, database


def executar_migracao_indice():
    with app.app_context():
        print("Iniciando injeção de índice de performance na tabela taxonomia...")

        try:
            # Comando SQL nativo para criar o índice focado em buscas textuais
            # O 'COLLATE NOCASE' obriga o SQLite a indexar o texto ignorando maiúsculas/minúsculas
            database.session.execute(database.text(
                "CREATE INDEX IF NOT EXISTS idx_taxonomia_nome ON taxonomia (nome COLLATE NOCASE);"
            ))
            database.session.commit()
            print("🌟 Sucesso! Índice 'idx_taxonomia_nome' injetado e ativado com segurança.")
            print("A tabela de taxonomia agora está otimizada para buscas rápidas em tempo de digitação.")

        except Exception as e:
            database.session.rollback()
            # Tratamento de segurança caso o índice por algum motivo já exista no arquivo físico
            if "already exists" in str(e).lower() or "duplicate" in str(e).lower():
                print("Aviso: O índice já existe no banco de dados. Nenhuma alteração foi necessária.")
            else:
                print(f"Erro crítico na migração do índice: {e}")


if __name__ == '__main__':
    executar_migracao_indice()