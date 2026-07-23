from feedin import database, app
# from feedin.modules.auth.models import ModCadastroCliente, ModFilaAtivacaoCliente
with app.app_context():
    database.create_all()
    print("Tabelas verificadas/criadas com sucesso!")

