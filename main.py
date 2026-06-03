from feedin import app, database

with app.app_context():
    database.create_all()
    print("Banco de dados verificado com sucesso!")

if __name__ == "__main__":
    app.run(debug=True, use_reloader=False, port=8000)