from datetime import datetime, timezone
from feedin.models import Usuario, IdentidadeCivil
from feedin.modules.auth.models import ModVinculoModulo
from feedin import database

# 🛑 IMPORTANTE: Importe o seu inicializador do Flask.
# Se você usa uma factory function (ex: create_app), importe ela.
# Se você cria o objeto 'app' direto no seu feedin/__init__.py, importe: from feedin import app
from feedin import app


def migrar_usuarios_validos_para_o_hub():
    """
    VARREDURA AUTOMÁTICA
    Varre usuários do Core com nível de acesso >= 10, localiza o elo civil (CPF Hash)
    e vincula nativamente ao módulo 'core' na tabela relacional.
    """
    print("🚀 [MIGRAÇÃO] Iniciando varredura de usuários válidos para o HUB...")

    usuarios_qualificados = database.session.query(Usuario).filter(Usuario.nivel_acesso >= 10).all()

    total_processado = 0
    total_criado = 0

    for usuario in usuarios_qualificados:
        total_processado += 1

        identidade = usuario.identidade

        if not identidade or not identidade.cpf_hash:
            print(
                f"⚠️ [MIGRAÇÃO] Usuário ID {usuario.id} ({usuario.email}) possui nível {usuario.nivel_acesso}, mas NÃO tem Identidade Civil criada. Pulando...")
            continue

        hash_limpo = str(identidade.cpf_hash).strip().lower()

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
                print(f"✅ [MIGRAÇÃO] Vínculo 'core' criado para: {usuario.username} (Hash: {hash_limpo[:8]}...)")
            except Exception as e:
                database.session.rollback()
                print(f"❌ [MIGRAÇÃO] Erro ao tentar inserir vínculo do usuário {usuario.id}: {e}")
                continue

    try:
        database.session.commit()
        print(f"\n🏁 [MIGRAÇÃO CONCLUÍDA]")
        print(f"📊 Total de usuários analisados: {total_processado}")
        print(f"✨ Novos vínculos inseridos com sucesso: {total_criado}")
    except Exception as e:
        database.session.rollback()
        print(f"❌ [MIGRAÇÃO] Erro crítico ao commitar lote no SQLite: {e}")


# ==============================================================================
# BLINDAGEM DO CONTEXTO DE APLICATIVO DO FLASK
# ==============================================================================
# ==============================================================================
# BLINDAGEM DO CONTEXTO DE APLICATIVO DO FLASK
# ==============================================================================
if __name__ == "__main__":
    # Como 'app' já veio pronto e configurado lá do 'from feedin import app',
    # nós NÃO chamamos 'app()'. Entramos direto no contexto dele:

    with app.app_context():
        migrar_usuarios_validos_para_o_hub()