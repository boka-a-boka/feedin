# main.py
from feedin import create_app, database


# Solicita a construção de uma instância limpa do aplicativo
app = create_app()

app.jinja_env.cache = {}  # Desativa o cache do Jinja em ambiente de desenvolvimento

with app.app_context():
    database.create_all()
    print("Banco de dados verificado com sucesso!")

if __name__ == "__main__":
    # use_reloader=False mantido conforme sua configuração de debug atual
    app.run(debug=True, use_reloader=False, port=8000)

