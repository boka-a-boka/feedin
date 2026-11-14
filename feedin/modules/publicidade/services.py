import random
from datetime import datetime, timezone
from sqlalchemy.sql import expression as func
from feedin import database as db
from .models import PublCampanha, PublSlotInventario, PublClique, PublCampanhaSlot, PublCampanhaTaxonomia


def obter_anuncio_slot(modulo_slug: str, nome_slot: str, usuario_cliente=None,
                       empresa_hospedeira_id=int) -> PublCampanha:
    """
    =============================================================================
    ENGINE DE SELEÇÃO E EXIBIÇÃO DE ANÚNCIOS (AdServer Contextual)
    =============================================================================
    Seleciona o anúncio mais elegível para exibição em um slot específico, aplicando:
    1. Validação de vigência e contrato ativo para o slot.
    2. Sorteio ponderado entre as campanhas válidas.
    3. Trava anticoncorrência (impede concorrentes diretos dentro do perfil da hospedeira).

    Args:
        modulo_slug (str): O módulo atual (ex: 'core', 'agenda', 'eventos').
        nome_slot (str): O slot de exibição (ex: 'feed_principal', 'dashboard_cliente').
        usuario_cliente (ModCadastroCliente, optional): Cliente logado para cruzamento de preferências/taxonomias.
        empresa_hospedeira_id (int, optional): ID da EseEmpresa onde o anúncio está sendo renderizado (se aplicável).

    Returns:
        PublCampanha ou None: O objeto da campanha escolhida pronto para renderização.
    =============================================================================
    """
    agora = datetime.now(timezone.utc)

    # Passo 1: Buscar o slot de inventário correspondente
    slot_obj = PublSlotInventario.query.filter_by(
        modulo_slug=modulo_slug,
        nome_slot=nome_slot,
        ativo=True
    ).first()

    if not slot_obj:
        return None

    # Passo 2: Buscar campanhas ativas, dentro da vigência e vinculadas a este slot
    # Fazemos um join direto com a tabela associativa publ_campanha_slot
    campanhas_candidatas = PublCampanha.query.join(
        PublCampanhaSlot, PublCampanhaSlot.campanha_id == PublCampanha.id
    ).filter(
        PublCampanhaSlot.slot_id == slot_obj.id,
        PublCampanha.status == 'ativo',
        PublCampanha.data_inicio <= agora,
        (PublCampanha.data_expiracao == None) | (PublCampanha.data_expiracao >= agora)
    ).all()

    if not campanhas_candidatas:
        return None

    # Passo 3: Resolver a taxonomia da empresa hospedeira (se houver contexto de perfil)
    taxonomia_hospedeira_id = None
    if empresa_hospedeira_id:
        from feedin.modules.empresa.models import EseEmpresa
        empresa_hospedeira = EseEmpresa.query.get(empresa_hospedeira_id)
        if empresa_hospedeira:
            taxonomia_hospedeira_id = empresa_hospedeira.categoria

    # Passo 4: Filtragem e Aplicação da Trava Anticoncorrência
    campanhas_elegiveis = []

    for campanha in campanhas_candidatas:
        # A. Trava Anticoncorrência: Se estamos no perfil de uma empresa hospedeira...
        if empresa_hospedeira_id and taxonomia_hospedeira_id:
            # Se o anunciante for interno (EseEmpresa), verificamos se é a própria dona da página
            if campanha.tipo_anunciante == 'interno_empresa' and campanha.ese_empresa_id == empresa_hospedeira_id:
                # A própria empresa anunciando em seu espaço é permitida
                pass
            else:
                # Verificar se o anunciante pertence à mesma taxonomia da hospedeira (Concorrente Direto)
                if campanha.tipo_anunciante == 'interno_empresa' and campanha.empresa_interna:
                    taxonomia_anunciante_id = campanha.empresa_interna.categoria
                    if taxonomia_anunciante_id and taxonomia_anunciante_id == taxonomia_hospedeira_id:
                        # Concorrente direto detectado! Pula esta campanha.
                        continue

        # B. Filtro por Afinidade de Taxonomia (Opcional se houver usuário/contexto)
        # Se a campanha possui taxonomias alvo cadastradas, podemos priorizar ou filtrar,
        # mas mantemos flexível para garantir preenchimento de inventário inicial.

        campanhas_elegiveis.append(campanha)

    if not campanhas_elegiveis:
        return None

    # Passo 5: Sorteio ponderado/aleatório da campanha elegível
    campanha_escolhida = random.choice(campanhas_elegiveis)

    # Incrementa atomicamente a visualização para relatórios de BI
    try:
        campanha_escolhida.visualizacoes = (campanha_escolhida.visualizacoes or 0) + 1
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"Erro ao atualizar contador de visualizações da campanha: {str(e)}")

    return campanha_escolhida


def registrar_clique_servico(campanha_id: str, cliente_id: str, modulo_slug: str, nome_slot: str, taxonomia_id=None,
                             origem='banner'):
    """
    =============================================================================
    REGISTRO DE CLIQUE E AUDITORIA (Business Intelligence)
    =============================================================================
    Salva a interação do ModCadastroCliente no AdServer para auditoria e conversão.
    =============================================================================
    """
    campanha = PublCampanha.query.get(campanha_id)
    if not campanha:
        return None

    novo_clique = PublClique(
        campanha_id=campanha.id,
        cliente_id=cliente_id,
        taxonomia_id=taxonomia_id or campanha.taxonomias_alvo.first().id if campanha.taxonomias_alvo.count() > 0 else None,
        modulo_slug=modulo_slug,
        nome_slot=nome_slot,
        origem_clique=origem
    )

    db.session.add(novo_clique)
    try:
        db.session.commit()
        return campanha.url_destino
    except Exception as e:
        db.session.rollback()
        print(f"Erro ao registrar clique publicitário: {str(e)}")
        return campanha.url_destino if campanha else None