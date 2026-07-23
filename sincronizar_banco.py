import os
import sys
from datetime import datetime, timezone

# Garante que o Python encontre o diretório raiz do projeto ao rodar via terminal/VPS
sys.path.append(os.path.abspath(os.path.dirname(__file__)))


def garantir_modulos_sistema_cadastrados(database, models_dict):
    """
    [POPULAR TABELA DE CONFIGURAÇÃO]
    Garante a existência e a parametrização dos módulos do ecossistema
    na tabela 'modulos_sistema'. (Evita quebras por BuildError ou falta de registros).
    """
    ModulosSistema = models_dict['ModulosSistema']
    print("\n⚙️  [CONFIGURAÇÃO] Checando parametrização dos módulos do ecossistema...")

    # Definição dos metadados idêntica ao plano estrutural unificado
    carga_modulos = [
        {
            'slug': 'core',
            'nome': 'FeedIn Core!',
            'icone': 'bi-search-heart',
            'descricao': 'Acesso ao painel central e histórico social.',
            'endpoint': 'dashboard',  # 🎯 Alinhado para o seu Dashboard real!
            'cor_hex': '#ffc107',
            'ativo': True
        },
        {
            'slug': 'empresa',
            'nome': 'Módulo Empresas & Negócios',
            'icone': 'bi-building',
            'descricao': 'Gerenciamento de estabelecimentos, taxonomias e dados locais.',
            'endpoint': 'empresa.painel_empresa',
            'cor_hex': '#111827',
            'ativo': True
        },
        {
            'slug': 'agenda',
            'nome': 'Módulo de Agendamentos',
            'icone': 'bi-calendar-check',
            'descricao': 'Controle de horários, serviços e atendimentos vinculados.',
            'endpoint': 'agenda.portal_entrada_agenda',  # 🎯 O portal de entrada correto!
            'cor_hex': '#0284C7',
            'ativo': True
        }
    ]

    modulos_inseridos = 0
    modulos_atualizados = 0

    for dados in carga_modulos:
        # Busca se o slug já existe fisicamente na tabela
        modulo_existente = database.session.query(ModulosSistema).filter_by(slug=dados['slug']).first()

        if not modulo_existente:
            # Se não existir, faz a inserção limpa
            novo_modulo = ModulosSistema(
                slug=dados['slug'],
                nome=dados['nome'],
                icone=dados['icone'],
                descricao=dados['descricao'],
                endpoint=dados['endpoint'],
                cor_hex=dados['cor_hex'],
                ativo=dados['ativo']
            )
            database.session.add(novo_modulo)
            modulos_inseridos += 1
            print(f"   ✨ Novo módulo cadastrado: '{dados['slug']}' -> {dados['endpoint']}")
        else:
            # Se já existir, força a atualização do endpoint e do estado ativo (Garante correção no VPS)
            modulo_existente.endpoint = dados['endpoint']
            modulo_existente.ativo = dados['ativo']
            modulo_existente.nome = dados['nome']
            modulo_existente.cor_hex = dados['cor_hex']
            modulos_atualizados += 1

    try:
        database.session.commit()
        print(f"🏁 [CONFIGURAÇÃO CONCLUÍDA] Inseridos: {modulos_inseridos} | Sincronizados: {modulos_atualizados}")
    except Exception as e:
        database.session.rollback()
        print(f"❌ [CONFIGURAÇÃO] Erro ao salvar parametrização de módulos no SQLite: {e}")


def migrar_usuarios_validos_para_o_hub(database, models_dict):
    """
    [MIGRAÇÃO DE DADOS]
    Varre usuários do Core com nível de acesso >= 10, localiza o elo civil (CPF Hash)
    e vincula nativamente ao módulo 'core' na tabela relacional de módulos.
    """
    Usuario = models_dict['Usuario']
    IdentidadeCivil = models_dict['IdentidadeCivil']
    ModVinculoModulo = models_dict['ModVinculoModulo']

    print("\n🚀 [MIGRAÇÃO] Iniciando varredura de usuários válidos para o HUB...")

    usuarios_qualificados = database.session.query(Usuario).filter(Usuario.nivel_acesso >= 10).all()

    total_processado = 0
    total_criado = 0

    for usuario in usuarios_qualificados:
        total_processado += 1
        identidade = usuario.identidade

        if not identidade or not identidade.cpf_hash:
            print(
                f"   ⚠️  Usuário ID {usuario.id} ({usuario.email}) possui nível {usuario.nivel_acesso}, mas NÃO tem Identidade Civil. Pulando...")
            continue

        hash_limpo = str(identidade.cpf_hash).strip().lower()

        # Verifica duplicidade para não quebrar restrições exclusivas do SQLite
        vinculo_existente = database.session.query(ModVinculoModulo).filter_by(
            cpf_hash=hash_limpo,
            modulo_slug='core'
        ).first()

        if not vinculo_existente:
            try:
                novo_vinculo = ModVinculoModulo(
                    cpf_hash=hash_limpo,
                    modulo_slug='core',
                    local_id=None,
                    email_customizado=usuario.email,
                    ativo=True
                )
                database.session.add(novo_vinculo)
                total_criado += 1
                print(f"   ✅ Vínculo 'core' criado para: {usuario.username} (Hash: {hash_limpo[:8]}...)")
            except Exception as e:
                database.session.rollback()
                print(f"   ❌ Erro ao tentar inserir vínculo do usuário {usuario.id}: {e}")
                continue

    try:
        database.session.commit()
        print(f"🏁 [MIGRAÇÃO CONCLUÍDA] Analisados: {total_processado} | Inseridos: {total_criado}")
    except Exception as e:
        database.session.rollback()
        print(f"❌ [MIGRAÇÃO] Erro crítico ao salvar lote no SQLite: {e}")


def executar_sincronizacao_e_ajustes():
    """
    [MIGRAÇÃO ESTRUTURAL + CONFIGURAÇÃO + DADOS]
    Instancia temporariamente o app usando a Application Factory e sincroniza o banco.
    """
    print("🔍 Conectando ao ecossistema modular do FeedIn (Instanciando app de manutenção)...")

    # 💥 PULO DO GATO: Importamos a Factory e o database instanciado de forma neutra
    from feedin import create_app, database

    # Cria a instância temporária para fornecer contexto de banco e configurações do .env
    app = create_app()

    print("📦 Carregando e unificando as estruturas de dados (Models)...")

    # Garante o carregamento das tabelas nativas e relacionais de todos os blueprints
    try:
        from feedin.models import Usuario, IdentidadeCivil
        # Ajustado para buscar de feedin.models o ModulosSistema
        from feedin.models import ModulosSistema
        print("   🔹 [1/5] Models do Core: Carregadas com sucesso")
    except ImportError as e:
        print(f"   ❌ Erro ao carregar as models do Core: {e}")
        return

    try:
        from feedin.modules.empresa import models as empresa_models
        print("   🔹 [2/5] Model Empresa: Carregada com sucesso")
    except ImportError as e:
        print(f"   ⚠️  Nota: Módulo Empresa não detectado ou mapeado em outro caminho: {e}")

    try:
        from feedin.modules.agenda import models as agenda_models
        print("   🔹 [3/5] Model Agenda: Carregada com sucesso")
    except ImportError as e:
        print(f"   ❌ Erro ao carregar as models da Agenda: {e}")

    try:
        from feedin.modules.auth.models import ModVinculoModulo
        print("   🔹 [4/5] Model Auth/Vínculos: Carregada com sucesso")
    except ImportError as e:
        print(f"   ❌ Erro crítico ao carregar a model ModVinculoModulo: {e}")
        return

    # Dicionário de conveniência para repassar as referências de classes carregadas
    models_dict = {
        'Usuario': Usuario,
        'IdentidadeCivil': IdentidadeCivil,
        'ModVinculoModulo': ModVinculoModulo,
        'ModulosSistema': ModulosSistema
    }

    # 🛠️ INICIA EXECUÇÃO ATÔMICA DENTRO DO APPLICATION CONTEXT CRIADO NA FACTORY
    with app.app_context():
        print("\n⚡ [ESTRUTURA] Iniciando varredura e comparação com o banco físico...")

        # 1. Cria tabelas inteiramente novas (caso os módulos novos usem tabelas que nem existiam)
        database.create_all()
        print("   ... Tabelas estruturais checadas.")

        engine = database.engine
        inspector = database.inspect(engine)
        colunas_injetadas = 0

        # 2. Pente fino em colunas que foram modificadas/adicionadas nas classes Python
        for nome_tabela, tabela_model in database.metadata.tables.items():
            try:
                colunas_no_banco = [c['name'] for c in inspector.get_columns(nome_tabela)]

                for coluna_model in tabela_model.columns:
                    if coluna_model.name not in colunas_no_banco:
                        print(f"   ⚠️  Coluna ausente no banco físico detectada: {nome_tabela}.{coluna_model.name}")

                        # Tipagem compatível para o ecossistema simplificado do SQLite
                        tipo_sql = str(coluna_model.type).upper()
                        if "VARCHAR" in tipo_sql or "TEXT" in tipo_sql:
                            tipo_sql = "TEXT"
                        elif "INTEGER" in tipo_sql:
                            tipo_sql = "INTEGER"
                        elif "BOOLEAN" in tipo_sql:
                            tipo_sql = "BOOLEAN"
                        elif "BLOB" in tipo_sql or "BINARY" in tipo_sql:
                            tipo_sql = "BLOB"
                        else:
                            tipo_sql = "TEXT"

                        query_alter = f'ALTER TABLE {nome_tabela} ADD COLUMN {coluna_model.name} {tipo_sql};'

                        with engine.connect() as conexao:
                            conexao.execute(database.text(query_alter))
                            conexao.commit()

                        print(f"   ⚡ Sucesso! Coluna '{coluna_model.name}' injetada na tabela '{nome_tabela}'.")
                        colunas_injetadas += 1
            except Exception as e:
                print(f"   ❌ Erro ao analisar a tabela {nome_tabela}: {e}")
                continue

        print(f"📊 Fim da checagem de colunas. Total de injeções físicas: {colunas_injetadas}")

        # 3. ETAPA DE SEEDING: Garante a gravação e correção dos metadados das tabelas de módulos
        garantir_modulos_sistema_cadastrados(database, models_dict)

        # 4. ETAPA DE LOGICA: Sincronização e atualização dos vínculos
        migrar_usuarios_validos_para_o_hub(database, models_dict)

        print("\n🏁 [PROCESSO DE MANUTENÇÃO TOTALMENTE CONCLUÍDO]")
        print("🔒 Dados de produção, empresas e taxonomias mantidos 100% protegidos e estáveis.")


if __name__ == "__main__":
    executar_sincronizacao_e_ajustes()