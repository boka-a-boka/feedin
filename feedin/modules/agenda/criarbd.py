from feedin import database, app
# from feedin.modules.agenda.models import ModCadastroCliente
with app.app_context():
    database.create_all()
    print("Tabelas verificadas/criadas com sucesso!")

