from datetime import datetime, timezone
from feedin import database as db


class PublCampanha(db.Model):
    """
    =============================================================================
    MÓDULO DE PUBLICIDADE: CAMPAINHAS E SLOTS (publ_campanha)
    =============================================================================
    Tabela centralizadora do novo Motor de Publicidade Universal do ecossistema FeedIn.
    Gerencia campanhas publicitárias dinâmicas, posicionamento em slots por módulo,
    vínculos com taxonomias globais (afastando concorrentes) e métricas de engajamento.

    Atributos:
        id (Integer): Chave primária única da campanha.
        local_id (Integer): Chave estrangeira para o estabelecimento anunciante (tabela 'local').
        taxonomia_id (Integer): Chave estrangeira para a taxonomia/tag global de afinidade.
        modulo_slug (String): Identificador único do módulo de origem (ex: 'agenda', 'empresa').
        nome_slot (String): Nome específico do espaço de exibição (ex: 'dashboard_cliente').
        url_flyer (String): Caminho relativo do arquivo de imagem otimizado (.webp).
        url_destino (String): Link externo de redirecionamento (WhatsApp, Instagram, Site).
        plano_marketing (String): Classificação comercial (ex: 'patrocinado', 'gratuito').
        status (String): Estado atual da campanha ('ativo', 'pausado', 'expirado').
        data_expiracao (DateTime): Data limite para exibição da publicidade.
        visualizacoes (Integer): Contador atômico de exibições do anúncio.
        created_at (DateTime): Data e hora de criação do registro no banco.
    =============================================================================
    """
    __tablename__ = 'publ_campanha'

    id = db.Column(db.Integer, primary_key=True)
    local_id = db.Column(db.Integer, db.ForeignKey('ese_empresa.id'), nullable=True)
    taxonomia_id = db.Column(db.Integer, db.ForeignKey('taxonomia.id'), nullable=False)

    modulo_slug = db.Column(db.String(30), nullable=False)
    nome_slot = db.Column(db.String(50), nullable=False)

    url_flyer = db.Column(db.String(255), nullable=True)
    url_destino = db.Column(db.String(255), nullable=False)

    plano_marketing = db.Column(db.String(20), default='gratuito')
    status = db.Column(db.String(20), default='ativo')
    data_expiracao = db.Column(db.DateTime, nullable=True)

    visualizacoes = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<PublCampanha #{self.id} - Slot: {self.nome_slot} (Módulo: {self.modulo_slug})>"


class PublClique(db.Model):
    """
    =============================================================================
    MÓDULO DE PUBLICIDADE: RASTREAMENTO DE CLIQUES / BI (publ_clique)
    =============================================================================
    Registra cada interação de clique em banners e flyers publicitários para fins
    de Business Intelligence, auditoria e relatórios de conversão por tag/origem.

    Atributos:
        id (Integer): Chave primária única do registro de clique.
        anuncio_id (Integer): Chave estrangeira para a campanha (`publ_campanha.id`).
        usuario_id (Integer): Chave estrangeira para o usuário que realizou o clique.
        taxonomia_id (Integer): Tag associada no momento da exibição/clique.
        origem_clique (String): Contexto ou tela de origem do clique (ex: 'flyer', 'dashboard').
        created_at (DateTime): Timestamp exato do momento do clique.
    =============================================================================
    """
    __tablename__ = 'publ_clique'

    id = db.Column(db.Integer, primary_key=True)
    anuncio_id = db.Column(db.Integer, db.ForeignKey('publ_campanha.id'), nullable=False)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=False)
    taxonomia_id = db.Column(db.Integer, db.ForeignKey('taxonomia.id'), nullable=False)
    origem_clique = db.Column(db.String(50), default='flyer')
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<PublClique #{self.id} - Anúncio ID: {self.anuncio_id} | Usuário ID: {self.usuario_id}>"


