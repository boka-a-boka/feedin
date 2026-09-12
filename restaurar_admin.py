from sqlalchemy import text
from datetime import datetime, timezone, time
from main import app, database as db
from feedin.modules.empresa.models import ColaboradorContrato, EseEmpresa
from feedin.models import Usuario
from feedin.modules.auth.models import ModCadastroCliente

print("🔄 [1/3] Atualizando a estrutura da tabela no SQLite...")

with app.app_context():
    with db.engine.connect() as conn:
        # Desativa chaves estrangeiras para permitir manipulação de tabelas
        conn.execute(text("PRAGMA foreign_keys=OFF;"))

        # 1. Faz cópia temporária da tabela
        conn.execute(
            text("CREATE TABLE IF NOT EXISTS colaborador_contratos_tmp AS SELECT * FROM colaborador_contratos;"))

        # 2. Dropa a tabela antiga
        conn.execute(text("DROP TABLE IF EXISTS colaborador_contratos;"))

        # Confirmar as alterações temporárias
        conn.commit()

    # 3. Recria a tabela com a nova estrutura do Model (onde id_cadastro_cliente é nullable=True)
    db.create_all()

    with db.engine.connect() as conn:
        # 4. Restaura os dados se a tabela temporária existir
        conn.execute(text("""
            INSERT INTO colaborador_contratos 
            SELECT * FROM colaborador_contratos_tmp;
        """))

        # 5. Remove a tabela temporária
        conn.execute(text("DROP TABLE IF EXISTS colaborador_contratos_tmp;"))

        # Reativa chaves estrangeiras
        conn.execute(text("PRAGMA foreign_keys=ON;"))

        # Efectua o commit final
        conn.commit()

print("✅ Tabela recriada e atualizada com sucesso no SQLite!")