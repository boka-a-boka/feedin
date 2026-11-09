# feedin/modules/registry_acoes.py

MODULOS_ACOES_REGISTRY = {
    'agenda': {
        'texto': 'Agendar Horário',
        'icone': 'bi-calendar-check-fill',
        'endpoint': 'agenda.dashboard', # Ou a rota pública de agendamento da empresa
        'cor': 'warning'
    },
    'pdv': {
        'texto': 'Comprar / Catálogo',
        'icone': 'bi-bag-check-fill',
        'endpoint': 'pdv.catalogo',
        'cor': 'success'
    },
    'hospedagem': {
        'texto': 'Reservar Acomodação',
        'icone': 'bi-journal-bookmark-fill',
        'endpoint': 'hospedagem.detalhes',
        'cor': 'primary'
    },
    'servicos': {
        'texto': 'Contratar Serviço',
        'icone': 'bi-file-earmark-text-fill',
        'endpoint': 'servicos.proposta',
        'cor': 'primary'
    }
}