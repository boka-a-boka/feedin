from main import app
from feedin import database as db
from sqlalchemy import text

with app.app_context():
    # Força a atualização de todos os valores da coluna pontos_fidelidade para um número Decimal válido
    db.session.execute(text("UPDATE ese_servico_oferecido SET pontos_fidelidade = 1.00 WHERE empresa_id = 2;"))
    db.session.commit()
    print("🎉 PRONTO! Banco de dados corrigido com sucesso!")