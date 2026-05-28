from feedin import database, app
# from feedin.models import Empresas, CategoriaNegocio
with app.app_context():
    database.create_all()
    print("Tabelas verificadas/criadas com sucesso!")

