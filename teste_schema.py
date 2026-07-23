# teste_schema.py
import os
from feedin import create_app, database as db
from feedin.modules.empresa.models import ColaboradorContrato
from feedin.modules.auth.models import ModCadastroCliente


def testar_criacao_tabelas():
    print("🔄 Inicializando o app do Flask no ambiente de desenvolvimento...")

    # Força o ambiente para desenvolvimento para usar a base correta
    os.environ['FLASK_ENV'] = 'development'

    app = create_app()

    with app.app_context():
        print("\n🔍 Verificando os modelos mapeados pelo SQLAlchemy:")
        # Lista as tabelas que o SQLAlchemy conhece neste momento
        tabelas_detectadas = db.metadata.tables.keys()
        print(f"Tabelas detectadas em memória: {list(tabelas_detectadas)}")

        if 'colaborador_contratos' not in tabelas_detectadas:
            print("\n❌ ERRO: A tabela 'colaborador_contratos' não foi detectada.")
            print(
                "Isso significa que a nova estrutura de Blueprints ainda não está importando os modelos automaticamente.")
            return

        print("\n🚀 Tentando executar db.create_all()...")
        try:
            # O create_all() não destrói dados; ele apenas cria o que não existe.
            # Como alteramos as colunas, se a tabela já existir no seu banco local,
            # ele NÃO vai alterá-la (o SQLAlchemy não faz ALTER TABLE nativo).
            # Para testar a estrutura pura, o ideal é rodar apontando para um banco temporário ou vazio.
            db.create_all()
            print("\n✅ Comando db.create_all() executado sem erros de compilação!")
            print("Verifique no seu gerenciador de banco de dados se a coluna 'id_cadastro_cliente' foi gerada.")

        except Exception as e:
            print(f"\n💥 Estourou um erro na execução do create_all():\n{str(e)}")


if __name__ == "__main__":
    testar_criacao_tabelas()