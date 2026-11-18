# main.py
from flask import render_template
from feedin import create_app, database


# Solicita a construção de uma instância limpa do aplicativo
app = create_app()

app.jinja_env.cache = {}  # Desativa o cache do Jinja em ambiente de desenvolvimento


@app.route("/agenda-pro")
@app.route("/campanha")
def campanha_agenda_pro():
    total_agenda_ativas = 0
    try:
        from feedin.modules.empresa.models import ModEmpresaModulo

        total_agenda_ativas = (
            ModEmpresaModulo.query.filter_by(
                modulo_slug="agenda",
                ativo=True,
            ).count()
        )
    except Exception:
        total_agenda_ativas = 0

    limite_lote_1 = 20
    vagas_restantes_lote_1 = max(0, limite_lote_1 - total_agenda_ativas)

    return render_template(
        "campanha/agenda_pro.html",
        total_agenda_ativas=total_agenda_ativas,
        limite_lote_1=limite_lote_1,
        vagas_restantes_lote_1=vagas_restantes_lote_1,
    )


with app.app_context():
    database.create_all()
    print("Banco de dados verificado com sucesso!")

if __name__ == "__main__":
    # use_reloader=False mantido conforme sua configuração de debug atual
    app.run(debug=False, use_reloader=False, port=8000)