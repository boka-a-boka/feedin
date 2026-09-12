from feedin import create_app, database as db

app = create_app()

with app.app_context():
    print("🧹 Iniciando limpeza forçada das tabelas de agendamento...")

    # 1. Desativa a checagem de Foreign Keys (funciona no SQLite e MySQL/Postgres)
    db.session.execute(db.text("PRAGMA foreign_keys = OFF;"))  # Se estiver usando SQLite
    # db.session.execute(db.text("SET FOREIGN_KEY_CHECKS = 0;")) # Descomente se for MySQL/MariaDB

    # 2. Força o DROP das tabelas de agendamento e itens
    db.session.execute(db.text("DROP TABLE IF EXISTS agh_agendamento_item;"))
    db.session.execute(db.text("DROP TABLE IF EXISTS agh_agendamento_rascunho_item;"))
    db.session.execute(db.text("DROP TABLE IF EXISTS agh_agendamento_rascunho;"))
    db.session.execute(db.text("DROP TABLE IF EXISTS agh_agendamento;"))

    # Se a tabela legada aghprofissional ou agh_profissional ainda existir no banco:
    db.session.execute(db.text("DROP TABLE IF EXISTS agh_profissional;"))
    db.session.execute(db.text("DROP TABLE IF EXISTS aghprofissional;"))

    db.session.commit()

    # 3. Reativa a checagem de Foreign Keys
    db.session.execute(db.text("PRAGMA foreign_keys = ON;"))  # SQLite
    # db.session.execute(db.text("SET FOREIGN_KEY_CHECKS = 1;")) # MySQL/MariaDB

    # 4. Recria as tabelas mapeadas nos seus Models atualizados
    db.create_all()

    print("✅ Tabelas recriadas com sucesso com a nova estrutura!")