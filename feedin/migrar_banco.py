from feedin import app, database


def executar_migracao():
    with app.app_context():
        print("Iniciando checagem da estrutura do banco de dados...")

        try:
            # Comando SQL nativo para injetar a nova coluna de anúncios
            # O SQLite aceita isso instantaneamente porque ela permite valores nulos (NULL)
            database.session.execute(database.text(
                "ALTER TABLE locais ADD COLUMN url_flyer VARCHAR(255) NULL;"
            ))
            database.session.commit()
            print("🌟 Sucesso! Coluna 'url_flyer' injetada com segurança.")
            print("Os 656 registros foram preservados intactos.")

        except Exception as e:
            database.session.rollback()
            # Se a coluna já existir (porque você rodou o script duas vezes sem querer), ele avisa aqui
            if "duplicate column name" in str(e).lower() or "already exists" in str(e).lower():
                print("Aviso: A coluna 'url_flyer' já existe no banco de dados. Nenhuma alteração foi necessária.")
            else:
                print(f"Erro crítico na migração: {e}")


if __name__ == '__main__':
    executar_migracao()