from sqlalchemy import text
from main import app, database as db

print("🔄 Copiando dados de 'colaborador_contratos_dg_tmp' para 'colaborador_contratos'...")

with app.app_context():
    with db.engine.connect() as conn:
        # Desativa chaves estrangeiras temporariamente para evitar falhas durante a migração
        conn.execute(text("PRAGMA foreign_keys=OFF;"))

        # 1. Copia todos os dados da tabela temporária para a nova tabela
        conn.execute(text("""
            INSERT INTO colaborador_contratos (
                id, 
                id_cadastro_cliente, 
                id_usuario, 
                id_local, 
                id_cargo, 
                papel_nome, 
                papel_nivel, 
                foto_profissional, 
                data_contratacao, 
                data_desligamento, 
                hora_inicio_expediente, 
                hora_fim_expediente, 
                hora_inicio_intervalo, 
                hora_fim_intervalo, 
                status_profissional
            )
            SELECT 
                id, 
                id_cadastro_cliente, 
                id_usuario, 
                id_local, 
                id_cargo, 
                papel_nome, 
                papel_nivel, 
                foto_profissional, 
                data_contratacao, 
                data_desligamento, 
                hora_inicio_expediente, 
                hora_fim_expediente, 
                hora_inicio_intervalo, 
                hora_fim_intervalo, 
                status_profissional
            FROM colaborador_contratos_dg_tmp;
        """))

        # 2. Exclui a tabela temporária
        conn.execute(text("DROP TABLE IF EXISTS colaborador_contratos_dg_tmp;"))

        # Reativa a verificação de chaves estrangeiras
        conn.execute(text("PRAGMA foreign_keys=ON;"))

        # Salva as alterações no banco SQLite
        conn.commit()

print("✅ Dados restaurados com sucesso na tabela 'colaborador_contratos' e tabela temporária removida!")