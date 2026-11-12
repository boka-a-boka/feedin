import sys
import os

# Garante que o diretório raiz do projeto seja o PRIMEIRO no path do Python
projeto_raiz = os.path.abspath(os.path.dirname(__file__))
if projeto_raiz in sys.path:
    sys.path.remove(projeto_raiz)
sys.path.insert(0, projeto_raiz)

from feedin import create_app, database
from feedin.models import Usuario, Selo, UsuarioSelo
from datetime import datetime, timezone

app = create_app()

with app.app_context():
    print("🔄 Garantindo a criação das tabelas no banco de dados...")
    database.create_all()
    print("✅ Tabelas verificadas/criadas com sucesso!")

    # 1. Busca o selo usando o codigo_interno correto ('pioneiro')
    selo_pioneiro = Selo.query.filter_by(codigo_interno='pioneiro').first()

    if not selo_pioneiro:
        print("❌ ERRO: O selo com codigo_interno='pioneiro' não foi encontrado na tabela 'selos'.")
    else:
        print(f"✅ Selo localizado: ID {selo_pioneiro.id} - {selo_pioneiro.nome}")

        # 2. Busca todos os usuários que já são pioneiros
        pioneiros = Usuario.query.filter_by(is_pioneiro=True).all()
        print(f"👥 Encontrados {len(pioneiros)} usuários com is_pioneiro=True.")

        contador_inseridos = 0

        for usuario in pioneiros:
            ja_tem = UsuarioSelo.query.filter_by(
                id_usuario=usuario.id,
                id_selo=selo_pioneiro.id
            ).first()

            if not ja_tem:
                novo_vinculo = UsuarioSelo(
                    id_usuario=usuario.id,
                    id_selo=selo_pioneiro.id,
                    data_conquista=usuario.data_cadastro or datetime.now(timezone.utc),
                    atribuido_por_admin=True
                )
                database.session.add(novo_vinculo)
                contador_inseridos += 1
                print(f"➕ Vinculando selo ao usuário: {usuario.username} (ID: {usuario.id})")

        database.session.commit()
        print(f"🎉 Processo concluído! {contador_inseridos} novos registros inseridos na tabela UsuarioSelo.")