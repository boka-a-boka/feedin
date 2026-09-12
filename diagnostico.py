from main import app
from feedin import database as db
from sqlalchemy import text

with app.app_context():
    print("\n--- INICIANDO VARREDURA DE DADOS CORROMPIDOS ---")

    # 1. Verifica todas as colunas da tabela ese_servico_oferecido
    rows = db.session.execute(
        text("SELECT id, empresa_id, taxonomia_id, pontos_fidelidade FROM ese_servico_oferecido")).fetchall()
    print(f"Total de registros em ese_servico_oferecido: {len(rows)}")
    for r in rows:
        val = r[3]  # pontos_fidelidade
        # Tenta converter para float como o SQLAlchemy tenta fazer
        try:
            if val is not None:
                float(val)
        except (ValueError, TypeError):
            print(
                f"❌ ENCONTRADO EM ese_servico_oferecido -> ID: {r[0]}, Empresa: {r[1]}, pontos_fidelidade inválido: '{val}' (tipo: {type(val)})")

    # 2. Corrigir TODOS os registros da tabela de uma vez sem filtrar por empresa_id
    db.session.execute(text("UPDATE ese_servico_oferecido SET pontos_fidelidade = 1.00;"))
    db.session.commit()
    print("✅ Todos os pontos_fidelidade da tabela ese_servico_oferecido foram resetados para 1.00!")