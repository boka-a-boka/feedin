from main import app
from feedin import database as db
from sqlalchemy import text

with app.app_context():
    print("--- CORRIGINDO TABELA ESE_SERVICO_PRECO ---")

    # 1. Atualiza registros que estejam nulos, vazios ou corrompidos para 0.00
    db.session.execute(text("""
        UPDATE ese_servico_preco 
        SET novo_valor = 0.00 
        WHERE novo_valor IS NULL 
           OR novo_valor = '' 
           OR typeof(novo_valor) = 'text';
    """))

    db.session.commit()
    print("✅ Tabela ese_servico_preco higienizada com sucesso!")