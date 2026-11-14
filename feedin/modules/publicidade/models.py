import uuid
from datetime import datetime, timezone
from feedin import database as db


class PublSlotInventario(db.Model):
    """
    =============================================================================
    CATÁLOGO DE INVENTÁRIO DE MÍDIA (publ_slot_inventario)
    =============================================================================
    Mapeia todos os espaços publicitários disponíveis em cada módulo do ecossistema
    FeedIn, definindo dimensões recomendadas, valores de referência e se o slot
    aceita campanhas de degustação.
    =============================================================================
    """
    __tablename__ = 'publ_slot_inventario'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    modulo_slug = db.Column(db.String(30), nullable=False)  # Ex: 'core', 'agenda', 'eventos'
    nome_slot = db.Column(db.String(50), nullable=False)  # Ex: 'feed_principal', 'dashboard_cliente'
    titulo_amigavel = db.Column(db.String(100), nullable=False)  # Ex: 'Topo do Feed Principal'

    dimensoes_recomendadas = db.Column(db.String(50), nullable=True)  # Ex: '1080x1080px (1:1)'
    valor_referencia_diario = db.Column(db.Float, default=0.0)  # Base para cálculo de propostas
    aceita_degustacao = db.Column(db.Boolean, default=True)
    ativo = db.Column(db.Boolean, default=True)

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<PublSlotInventario [{self.modulo_slug}] {self.nome_slot}>"


class PublCampanha(db.Model):
    """
    =============================================================================
    CAMPAINHAS PUBLICITÁRIAS (publ_campanha)
    =============================================================================
    Tabela centralizadora de contratos de publicidade. Suporta os 3 perfis de anunciantes:
    - Perfil A: Anunciante Nativo (vinculado a 'ese_empresa_id')
    - Perfil B: Prestador Autônomo Local (anunciante externo com documento)
    - Perfil C: Marca Corporativa / Global (gerenciado pelo Admin Master)
    =============================================================================
    """
    __tablename__ = 'publ_campanha'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    # TIPAGEM DO ANUNCIANTE
    tipo_anunciante = db.Column(db.String(30), default='interno_empresa', nullable=False)
    # Opções: 'interno_empresa', 'autonomo_local', 'corporativo_global'

    # Vínculo para Perfil A (Lojista interno do FeedIn)
    ese_empresa_id = db.Column(db.Integer, db.ForeignKey('ese_empresa.id'), nullable=True)

    # Vínculos para Perfis B e C (Sem perfil comercial completo de PWA)
    nome_anunciante_externo = db.Column(db.String(150), nullable=True)
    documento_anunciante = db.Column(db.String(18), nullable=True)  # CPF ou CNPJ higienizado

    # MÍDIA E DESTINO
    url_flyer = db.Column(db.String(255), nullable=True)  # Caminho do WebP otimizado
    url_destino = db.Column(db.String(255), nullable=False)  # Link externo (WhatsApp, Site, Instagram)

    # GESTÃO COMERCIAL E VIGÊNCIA ELÁSTICA
    plano_marketing = db.Column(db.String(30), default='degustacao')  # 'degustacao', 'bonificado', 'pago'
    status = db.Column(db.String(20), default='ativo')  # 'ativo', 'pausado', 'expirado'

    data_inicio = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    data_expiracao = db.Column(db.DateTime, nullable=True)  # Prazo elástico configurável

    visualizacoes = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    # RELACIONAMENTOS ORM
    empresa_interna = db.relationship('EseEmpresa', foreign_keys=[ese_empresa_id], backref='campanhas_publicitarias',
                                      lazy=True)

    # Relações N para N via tabelas associativas
    slots_contratados = db.relationship('PublSlotInventario', secondary='publ_campanha_slot',
                                        backref='campanhas_vinculadas', lazy='dynamic')
    taxonomias_alvo = db.relationship('Taxonomia', secondary='publ_campanha_taxonomia', backref='campanhas_alvo',
                                      lazy='dynamic')

    def __repr__(self):
        nome = self.empresa_interna.nome if self.empresa_interna else self.nome_anunciante_externo
        return f"<PublCampanha #{self.id[:8]} | Anunciante: {nome} | Status: {self.status}>"


# TABELAS ASSOCIATIVAS (Pivôs para Relacionamentos N para N)

class PublCampanhaSlot(db.Model):
    """Associa uma campanha aos múltiplos slots/espaços contratados."""
    __tablename__ = 'publ_campanha_slot'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    campanha_id = db.Column(db.String(36), db.ForeignKey('publ_campanha.id', ondelete='CASCADE'), nullable=False)
    slot_id = db.Column(db.String(36), db.ForeignKey('publ_slot_inventario.id', ondelete='CASCADE'), nullable=False)


class PublCampanhaTaxonomia(db.Model):
    """Associa uma campanha às taxonomias/tags globais de afinidade e público-alvo."""
    __tablename__ = 'publ_campanha_taxonomia'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    campanha_id = db.Column(db.String(36), db.ForeignKey('publ_campanha.id', ondelete='CASCADE'), nullable=False)
    taxonomia_id = db.Column(db.Integer, db.ForeignKey('taxonomia.id', ondelete='CASCADE'), nullable=False)


class PublClique(db.Model):
    """
    =============================================================================
    AUDITORIA E BUSINESS INTELLIGENCE DE CLIQUES (publ_clique)
    =============================================================================
    Registra cada interação do usuário do módulo (ModCadastroCliente) para geração
    de relatórios de conversão e comprovação de valor ao fim da degustação.
    =============================================================================
    """
    __tablename__ = 'publ_clique'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    campanha_id = db.Column(db.String(36), db.ForeignKey('publ_campanha.id', ondelete='CASCADE'), nullable=False)

    # Identificação unificada do cliente do módulo (UUID char(36)) via ModCadastroCliente
    cliente_id = db.Column(db.String(36), db.ForeignKey('mod_cadastro_cliente.id', ondelete='CASCADE'), nullable=False)

    taxonomia_id = db.Column(db.Integer, db.ForeignKey('taxonomia.id', ondelete='CASCADE'),
                             nullable=True)  # Tag contextual do clique

    modulo_slug = db.Column(db.String(30), nullable=False)  # Módulo onde o clique ocorreu
    nome_slot = db.Column(db.String(50), nullable=False)  # Slot específico que exibiu o banner
    origem_clique = db.Column(db.String(50), default='banner')

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    # Relacionamentos ORM atualizados para o padrão ModCadastroCliente
    campanha = db.relationship('PublCampanha',
                               backref=db.backref('registros_cliques', lazy='dynamic', cascade='all, delete-orphan'))
    cliente = db.relationship('ModCadastroCliente', backref='cliques_publicidade_realizados', lazy=True)

    def __repr__(self):
        return f"<PublClique #{self.id[:8]} | Campanha ID: {self.campanha_id[:8]} | Cliente ID: {self.cliente_id[:8]}>"