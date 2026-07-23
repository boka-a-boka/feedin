import sys
import os
from datetime import date, time
from sqlalchemy import text

# Garante a raiz no caminho
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from feedin import create_app, database

app = create_app()

with app.app_context():
    print("Conectando direto ao motor do banco...")
    
    # Executa SQL puro direto na engine para ignorar as models e rotas do Python
    with database.engine.connect() as conexao:
        # 1. Limpa registros anteriores para evitar duplicidade
        conexao.execute(
            text("DELETE FROM colaborador_contratos WHERE id_usuario = 2 AND id_local = 576")
        )
        
        # 2. Insere o contrato master do proprietário
        conexao.execute(
            text("""
                INSERT INTO colaborador_contratos 
                (id_usuario, id_local, papel_nome, papel_nivel, status_profissional, data_contratacao, hora_inicio_expediente, hora_fim_expediente)
                VALUES 
                (2, 576, 'proprietario', 999, 'ativo', :data_hoje, :ini_exp, :fim_exp)
            """),
            {
                "data_hoje": date.today(),
                "ini_exp": time(8, 0).strftime('%H:%M:%S'),
                "fim_exp": time(18, 0).strftime('%H:%M:%S')
            }
        )
        # Força o commit na conexão bruta
        conexao.commit()
        
    print("\n>>> SUCESSO ABSOLUTO: CONTRATO INJETADO VIA SQL BRUTO! <<< \n")