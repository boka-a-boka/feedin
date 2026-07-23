# feedin/modules/empresa/models.py
import re
import hashlib
import uuid
import enum
from feedin import database as db
from datetime import datetime, timezone

# =====================================================================
# 🏛️ ENTIDADES CORE E PERIFÉRICAS DO MÓDULO
# =====================================================================

"""
==========================================================================================
📌 MÓDULO EMPRESA: ARQUITETURA CORE DE GOVERNANÇA, NEGÓCIOS E MEMÓRIA URBANA
==========================================================================================
Este arquivo centraliza a inteligência corporativa e de marcas do FeedIn.
A arquitetura foi projetada para garantir:
  1. Hibridismo de Negócios: Suporta empresas físicas (atreladas a 'locais.id') e 
     empresas 100% digitais (onde local_id é Null e valida-se o 'dominio_web').
  2. Autonomia Territorial e Histórica: Cada estabelecimento físico gera uma nova linha 
     na tabela 'locais' (id único). A união e o histórico de empresas que ocuparam o 
     mesmo espaço ao longo do tempo serão consolidados via metadados do Google Maps (google_place_id).
  3. Segurança Jurídica (Claim Self-Service): Trilhas de auditoria estritas com IP, data 
     e termos de responsabilidade aceitos para mitigar fraudes e perfis falsos.

Tabelas Gerenciadas:
  - EseEmpresa: Cadastro mestre de identidade jurídica, branding PWA e compliance.
==========================================================================================
"""

class EseEmpresa(db.Model):
    """
    📌 ESE_EMPRESA: O Hub Comercial e de Branding do Empreendedor
    --------------------------------------------------------------------------------------
    Centraliza a operação do lojista ou prestador de serviços. Conecta o proprietário (999)
    ao seu ponto físico ou canal digital, injetando as cores e a história diretamente na
    moldura do PWA do FeedIn.
    """
    __tablename__ = 'ese_empresa'
    __table_args__ = {'extend_existing': True}

    id = db.Column(db.Integer, primary_key=True)

    # 🔗 OS DOIS ELOS VITAIS (Ajustados para Governança e Negócios Digitais):
    proprietario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=False)
    local_id = db.Column(db.Integer, db.ForeignKey('locais.id'), nullable=True)

    # -------------------------------------------------------------
    # SEÇÃO A: IDENTIDADE JURÍDICA E COMERCIAL (A BASE DO CORE)
    # -------------------------------------------------------------
    nome = db.Column(db.String(100), nullable=False)
    slug = db.Column(db.String(100), unique=True)
    logomarca = db.Column(db.String(255), nullable=True)
    fachada = db.Column(db.String(255), nullable=True)

    # 🎯 AJUSTE DA TAXONOMIA: O campo agora reflete explicitamente a relação com o Core
    categoria = db.Column(db.Integer, db.ForeignKey('taxonomia.id'), nullable=True)

    # Campos para contemplar Pessoa Física (CPF) e Jurídica (CNPJ) higienizados (apenas números)
    documento_oficial = db.Column(db.String(14), unique=True, nullable=True)
    tipo_documento = db.Column(db.String(4), nullable=True)  # 'CPF' ou 'CNPJ'

    # O marco zero cronológico da empresa
    data_fundacao = db.Column(db.Date, nullable=True)

    # -------------------------------------------------------------
    # SEÇÃO B: LINHA DO TEMPO E PROFUNDIDADE IDENTITÁRIA (HISTÓRIA)
    # -------------------------------------------------------------
    dominio_web = db.Column(db.String(255), unique=True, nullable=True)
    historia_ocupacao = db.Column(db.Text, nullable=True)
    missao_valores = db.Column(db.Text, nullable=True)

    # Mecanismos do Claim Self-Service (Segurança e Auto-responsabilidade jurídica)
    termo_responsabilidade_aceito = db.Column(db.Boolean, default=False, nullable=False)
    data_aceite_termo = db.Column(db.DateTime, nullable=True)
    ip_aceite_termo = db.Column(db.String(45), nullable=True)
    status_homologacao = db.Column(db.String(30), default='aguardando_dados')

    # -------------------------------------------------------------
    # SEÇÃO C: CONFIGURAÇÕES DE AMBIENTE (MOLDURA DO PWA E HUB OPERACIONAL)
    # -------------------------------------------------------------
    cor_primaria = db.Column(db.String(7), default="#111827")
    cor_secundaria = db.Column(db.String(7), default="#6B7280")

    # Relacionamentos explícitos mantidos e protegidos
    proprietario = db.relationship('Usuario', backref=db.backref('minhas_empresas', lazy=True))
    local_fisico = db.relationship('Local', backref=db.backref('empresas_instaladas', lazy=True))

    # 🎯 RELACIONAMENTO COM A TAXONOMIA: Mapeamento virtual para o ecossistema comercial
    categoria_rel = db.relationship('Taxonomia', backref=db.backref('empresas_vinculadas', lazy=True))

    def __repr__(self):
        if self.local_id:
            return f"<EseEmpresa {self.nome} - Instalada no Local ID: {self.local_id}>"
        return f"<EseEmpresa {self.nome} - Sede Digital (Domínio: {self.dominio_web})>"

    # -------------------------------------------------------------
    # MÓDULOS DE VALIDAÇÃO E REGRAS DE NEGÓCIO
    # -------------------------------------------------------------
    @staticmethod
    def validar_dominio_proprio(url):
        if not url:
            return True

        redes_proibidas = [
            r'instagram\.com', r'facebook\.com', r'fb\.com', r'tiktok\.com',
            r'twitter\.com', r'x\.com', r'linkedin\.com', r'kwai\.com'
        ]

        url_lower = url.lower()
        for padrao in redes_proibidas:
            if re.search(padrao, url_lower):
                raise ValueError(
                    "Redes sociais não são aceitas como domínio de validação do negócio. Use um domínio próprio.")
        return True


class UsuarioFavorito(db.Model):
    """Registra os favoritamentos do usuário para personalizar o FeedIn Negócios."""
    __tablename__ = 'usuario_favorito'
    __table_args__ = (
        db.UniqueConstraint('usuario_id', 'empresa_id', name='unique_usuario_empresa_fav'),
        db.UniqueConstraint('usuario_id', 'segmento_id', name='unique_usuario_segmento_fav'),
        {'extend_existing': True}
    )

    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=False)
    empresa_id = db.Column(db.Integer, db.ForeignKey('ese_empresa.id'), nullable=True)
    segmento_id = db.Column(db.Integer, db.ForeignKey('taxonomia.id'), nullable=True)

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<UsuarioFavorito User:{self.usuario_id} Empresa:{self.empresa_id}>"

class EseProcessoClaim(db.Model):
    __tablename__ = 'ese_processo_claim'

    id = db.Column(db.Integer, primary_key=True)

    # Elo com o Local do Core que está sendo disputado
    local_id = db.Column(db.Integer, db.ForeignKey('locais.id'), nullable=False)

    # Quem iniciou o processo (Pode ser o current_user do Core se logado)
    usuario_solicitante_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=True)

    # Canal escolhido para receber o link (WhatsApp ou E-mail)
    canal_comunicacao = db.Column(db.String(20), nullable=False)  # 'whatsapp' ou 'email'
    alvo_comunicacao = db.Column(db.String(120), nullable=False)  # O número ou e-mail exato enviado

    # Dados temporários fornecidos no Passo 1 (Preservados para histórico/auditoria)
    documento_declarado = db.Column(db.String(14), nullable=False)
    tipo_documento_declarado = db.Column(db.String(4), nullable=False)  # 'CPF' ou 'CNPJ'
    telefone_declarado = db.Column(db.String(20), nullable=False)
    email_declarado = db.Column(db.String(120), nullable=False)

    # Controle de Tempo e Segurança (TTL de 5 dias)
    token_validacao = db.Column(db.String(32), unique=True, nullable=False)  # UUID32 enviado no link
    data_inicio = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    data_limite = db.Column(db.DateTime, nullable=False)  # data_inicio + 5 dias

    # Rastreabilidade de Segurança contra Fraudes
    ip_solicitacao = db.Column(db.String(45), nullable=True)

    # Estados da Esteira de Reivindicação
    # 'em_andamento': Bloqueia o Local no Core (Janela de 5 dias)
    # 'concluido': Claim finalizado com sucesso, gerou EseEmpresa
    # 'expirado': Passou dos 5 dias. Local liberado, mas este registro fica guardado.
    # 'suspeito_bloqueado': Administração travou por indício de fraude/engraçadinho.
    status_processo = db.Column(db.String(30), default='em_andamento', index=True)

    # Relacionamento para auditoria rápida
    local = db.relationship('Local', backref=db.backref('historico_claims', lazy='dynamic'))

    def __repr__(self):
        return f'<EseProcessoClaim Local ID {self.local_id} - Status {self.status_processo}>'


class HistoricoAlteracaoLocal(db.Model):
    __tablename__ = 'historico_alteracao_local'

    id = db.Column(db.Integer, primary_key=True)
    local_id = db.Column(db.Integer, db.ForeignKey('locais.id'), nullable=False)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=False)

    # Snapshot completo do estado anterior em formato String/Texto (JSON)
    dados_anteriores = db.Column(db.Text, nullable=False)

    data_alteracao = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    prazo_expiracao = db.Column(db.DateTime, nullable=False)  # Data limite (Data Atual + 5 dias)
    status_pendencia = db.Column(db.String(20), default='pendente')  # 'pendente', 'concluido', 'revertido'

    # Relacionamento para facilitar consultas
    local = db.relationship('Local', backref=db.backref('historico_seguranca', lazy='dynamic'))


class ModHomologacaoEmpresa(db.Model):
    """Tabela de segurança jurídica e compliance do FeedIn."""
    __tablename__ = 'mod_homologacao_empresa'
    __table_args__ = {'extend_existing': True}

    id = db.Column(db.Integer, primary_key=True)
    id_local = db.Column(db.Integer, db.ForeignKey('locais.id'), nullable=False)

    path_comprovante_endereco = db.Column(db.String(255), nullable=False)
    path_cartao_cnpj_ou_social = db.Column(db.String(255), nullable=False)

    data_envio = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    data_analise = db.Column(db.DateTime, nullable=True)
    id_auditor_feedin = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=True)

    status_auditoria = db.Column(db.String(20), default='pendente', nullable=False)
    motivo_rejeicao = db.Column(db.Text, nullable=True)

    local = db.relationship('Local', backref='historico_homologacao')

    def __repr__(self):
        return f"<ModHomologacaoEmpresa Local ID: {self.id_local} - Status: {self.status_auditoria}>"


class ColaboradorContrato(db.Model):
    """
    O Histórico Profissional do Usuário baseado no Perfil de Módulos.
    Vincula diretamente o colaborador (ModCadastroCliente) ao local de trabalho.
    """
    __tablename__ = 'colaborador_contratos'

    id = db.Column(db.Integer, primary_key=True)

    # 🎯 O ELO CORRETO: Chave estrangeira de 36 caracteres apontando para o Cadastro de Módulo
    id_cadastro_cliente = db.Column(db.String(36), db.ForeignKey('mod_cadastro_cliente.id'), nullable=False)

    # Referência ao Core (Mantida temporariamente ou como nullable=True para auditoria)
    id_usuario = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=True)

    id_local = db.Column(db.Integer, db.ForeignKey('locais.id'), nullable=False)
    id_cargo = db.Column(db.Integer, db.ForeignKey('cargos.id'), nullable=False)

    papel_nome = db.Column(db.String(30), nullable=False, default='operador')
    papel_nivel = db.Column(db.Integer, nullable=False, default=500)

    # Linha do Tempo Profissional
    data_contratacao = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    data_desligamento = db.Column(db.DateTime, nullable=True)

    # Configuração de Expediente Diário
    hora_inicio_expediente = db.Column(db.Time, nullable=False)
    hora_fim_expediente = db.Column(db.Time, nullable=False)
    hora_inicio_intervalo = db.Column(db.Time, nullable=True)
    hora_fim_intervalo = db.Column(db.Time, nullable=True)

    status_profissional = db.Column(db.String(20), default='ativo')

    # 🌟 Relacionamentos atualizados para o padrão correto
    cadastro_modulo = db.relationship('ModCadastroCliente', backref='contratos_trabalho')
    local = db.relationship('Local', backref='equipe_colaboradores')
    cargo = db.relationship('Cargo')

    # Mantido apenas para compatibilidade legada se necessário
    usuario = db.relationship('Usuario', backref='contratos_core_legados')


class ColaboradorDetalhesPessoais(db.Model):
    """
    📌 COLABORADOR_DETALHES_PESSOAIS: Ficha de Autodeclaração do Trabalhador
    --------------------------------------------------------------------------------------
    Guarda as informações civis, médicas, de segurança e operacionais do trabalhador
    preenchidas e autorizadas por ele após o aceite do convite de trabalho.
    """
    __tablename__ = 'colaborador_detalhes_pessoais'

    id = db.Column(db.Integer, primary_key=True)
    # Vinculado diretamente ao contrato de trabalho gerado APÓS o aceite explícito
    contrato_id = db.Column(db.Integer, db.ForeignKey('colaborador_contratos.id'), nullable=False, unique=True)

    # 👤 Dados Pessoais de Exibição e Civis
    nome_completo = db.Column(db.String(150), nullable=False)
    nome_exibicao_pwa = db.Column(db.String(50), nullable=False)  # Nome profissional/crachá digital
    cpf = db.Column(db.String(14), nullable=True)    # Se Pessoa Física
    cnpj = db.Column(db.String(18), nullable=True)   # Se Pessoa Jurídica (MEI/Prestador)
    estado_civil = db.Column(db.String(30), nullable=True)

    # 📍 Endereço Completo (Coletado sob demanda do vínculo operacional)
    logradouro = db.Column(db.String(150), nullable=False)
    numero = db.Column(db.String(10), nullable=False)
    complemento = db.Column(db.String(100), nullable=True)
    bairro = db.Column(db.String(80), nullable=False)
    cidade = db.Column(db.String(100), nullable=False)
    estado = db.Column(db.String(2), nullable=False)  # UF
    cep = db.Column(db.String(9), nullable=False)

    # 📞 Contatos de Segurança e Saúde (Acesso rápido em emergências)
    telefone_pessoal = db.Column(db.String(20), nullable=False)
    contato_emergencia_nome = db.Column(db.String(100), nullable=True)
    contato_emergencia_fone = db.Column(db.String(20), nullable=True)
    tipo_sanguineo = db.Column(db.String(5), nullable=True)  # Ex: 'A+', 'O-'

    # 👕 Vestuário e Uniformes (Tratamento operacional)
    tamanho_camiseta = db.Column(db.String(10), nullable=True)  # Ex: 'PP', 'P', 'M', 'G', 'GG', 'XGG'
    tamanho_calca = db.Column(db.String(10), nullable=True)     # Ex: '38', '40', '42', '44' ou 'M', 'G'
    tamanho_calcado = db.Column(db.String(5), nullable=True)     # Ex: '37', '38', '39', '40'

    # 🏦 Dados Bancários (Para repasses/comissões)
    banco_nome = db.Column(db.String(100), nullable=True)
    agencia = db.Column(db.String(10), nullable=True)
    conta_corrente = db.Column(db.String(20), nullable=True)
    chave_pix = db.Column(db.String(100), nullable=True)

    criado_em = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    # Relacionamento com o Contrato
    contrato = db.relationship(
        'ColaboradorContrato',
        backref=db.backref('detalhes_pessoais', uselist=False, cascade='all, delete-orphan')
    )

    def __repr__(self):
        return f"<DetalhesPessoais Colaborador {self.nome_exibicao_pwa} - Contrato ID {self.contrato_id}>"


"""
==========================================================================================
📌 MÓDULO EMPRESA: ARQUITETURA DE MODELOS DE DADOS & PERSISTÊNCIA JURÍDICA
==========================================================================================
Este arquivo centraliza a modelagem de dados do ecossistema corporativo do FeedIn.
A arquitetura foi projetada para garantir:
  1. Integridade Territorial: Vinculação estrita a locais físicos mapeados em Piracicaba.
  2. Compliance Progressivo: Ativação formal de vínculos mediante envio de ativos digitais.
  3. Flexibilidade Temporal: Separação entre funcionamento comercial e escalas de trabalho.
  4. Inteligência de Calendário: Gestão automatizada de feriados civis com controle de exceções.

Tabelas Gerenciadas:
  - CadastroFeriado: Cache anual de feriados nacionais, estaduais e municipais.
  - EmpresaCalendarioExcecao: Regras de exceção (abrir/fechar/horário reduzido) por empresa.
  - EseHorarioFuncionamento: Grade padrão de funcionamento do estabelecimento (0-6).
  - EscalaTrabalhoColaborador: Escalas e turnos variáveis dos prestadores de serviço.
==========================================================================================
"""

class CadastroFeriado(db.Model):
    """
    📌 CADASTRO_FERIADO: Base de Dados Central de Calendário Civil
    --------------------------------------------------------------------------------------
    Armazena o cache dos feriados oficiais para evitar consultas repetitivas a APIs externas.
    Higienizada anualmente para manter a performance do banco de dados otimizada.
    """
    __tablename__ = 'cadastro_feriado'

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False)  # Ex: "Independência do Brasil", "Aniversário de Piracicaba"
    data = db.Column(db.Date, nullable=False, unique=True)  # Trava de unicidade temporal
    abrangencia = db.Column(db.String(20), default='nacional')  # 'nacional', 'estadual', 'municipal'
    localidade = db.Column(db.String(50), default='BR')  # 'BR', 'SP', 'Piracicaba'

    def __repr__(self):
        return f"<CadastroFeriado: {self.nome} em {self.data}>"


class EmpresaCalendarioExcecao(db.Model):
    """
    📌 EMPRESA_CALENDARIO_EXCECAO: Camada de Flexibilidade Corporativa (Switch de Feriados)
    --------------------------------------------------------------------------------------
    Permite que o gestor da empresa decline do fechamento padrão em um feriado oficial e
    estabeleça que irá trabalhar, ou crie emendas e pontes customizadas para o seu negócio.
    """
    __tablename__ = 'empresa_calendario_excecao'

    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('ese_empresa.id'), nullable=False)
    feriado_id = db.Column(db.Integer, db.ForeignKey('cadastro_feriado.id'),
                           nullable=True)  # Opcional se for emenda interna

    data_excecao = db.Column(db.Date, nullable=False)

    # REGRA DE OURO: True = Estabelecimento optou por trabalhar / False = Estabelecimento optou por fechar
    trabalha_no_dia = db.Column(db.Boolean, default=False, nullable=False)

    # Suporte a horários especiais reduzidos em dias festivos (Ex: Véspera de Natal)
    horario_abertura_excecao = db.Column(db.Time, nullable=True)
    horario_fechamento_excecao = db.Column(db.Time, nullable=True)

    # Relacionamentos
    feriado_oficial = db.relationship('CadastroFeriado', backref='excecoes_empresas')


class EseHorarioFuncionamento(db.Model):
    """
    📌 ESE_HORARIO_FUNCIONAMENTO: Grade Comercial Semanal do Estabelecimento
    --------------------------------------------------------------------------------------
    Gerencia as faixas horárias tradicionais da empresa (Segunda a Domingo).
    Suporta múltiplos turnos por dia (ex: fechamento para almoço) através do 'periodo_id'.
    Usado diretamente na inteligência visual do PWA para calcular o status "Aberto Agora".
    """
    __tablename__ = 'ese_horario_funcionamento'

    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('ese_empresa.id'), nullable=False)

    dia_semana = db.Column(db.Integer, nullable=False)  # 0=Domingo, 1=Segunda, 2=Terça... 6=Sábado
    horario_abertura = db.Column(db.Time, nullable=False)
    horario_fechamento = db.Column(db.Time, nullable=False)
    periodo_id = db.Column(db.Integer, default=1)  # Diferencia Turno 1 (Manhã) de Turno 2 (Tarde)


class EscalaTrabalhoColaborador(db.Model):
    """
    Representa a matriz volátil e o histórico de escalas de trabalho dos colaboradores.

    Esta model resolve o engessamento de horários fixos, permitindo que a jornada padrão
    estabelecida no contrato seja sobreposta de forma dinâmica por períodos específicos
    (um dia isolado ou intervalo de datas) para tratar exceções, trocas de turno ou faltas.

    Regra de Ouro do Motor de Busca (Resolução de Conflitos):
    Ao calcular a disponibilidade ou renderizar a agenda de um colaborador para o dia X,
    o sistema deve buscar registros ativos filtrando por `data_inicio <= X <= data_fim`.
    Havendo concorrência de registros, aplica-se a seguinte precedência de sobreposição:
    1. 'emergencial' (Maior prioridade: reações de última hora)
    2. 'alternativo' (Média prioridade: planejamentos temporários/férias)
    3. 'padrao'      (Menor prioridade: espelho da base contratual)

    O primeiro registro encontrado seguindo essa ordem anula os demais para o dia calculado.
    """
    __tablename__ = 'escala_trabalho_colaborador'

    id = db.Column(db.Integer, primary_key=True)
    contrato_id = db.Column(db.Integer, db.ForeignKey('colaborador_contratos.id'), nullable=False)

    # 🗓️ Novo campo: Mapeamento de dia da semana (0=Segunda, 6=Domingo ou 1=Segunda, 7=Domingo)
    # Nullable=True porque escalas 'alternativo' ou 'emergencial' usam intervalo de datas (data_inicio / data_fim)
    dia_semana = db.Column(db.Integer, nullable=True)

    # Controle temporal da escala
    data_inicio = db.Column(db.Date, nullable=False)  # Para escala padrão, pode ser a data de criação/contrato
    data_fim = db.Column(db.Date, nullable=False)  # Para escala padrão, pode ser 2099-12-31 ou aberta

    # Flag de Controle
    tipo_escala = db.Column(db.String(20), default='padrao', nullable=False)

    # Janela de Expediente
    inicio_expediente = db.Column(db.Time, nullable=False)
    fim_expediente = db.Column(db.Time, nullable=False)

    # Janela de Intervalo
    inicio_intervalo = db.Column(db.Time, nullable=True)
    fim_intervalo = db.Column(db.Time, nullable=True)

    ativo = db.Column(db.Boolean, default=True, nullable=False)
    observacao = db.Column(db.Text, nullable=True)  # Livre para anotações REAIS (ex: "Troca de folga")

# 🕵️ Auditabilidade Leve via ModCadastroCliente (UUID)
    atualizado_por_id = db.Column(db.String(36), db.ForeignKey('mod_cadastro_cliente.id'), nullable=True)
    atualizado_em = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relacionamento opcional para facilidade de acesso no Jinja/Python (ex: escala.editor.nome_completo)
    editor = db.relationship('ModCadastroCliente', foreign_keys=[atualizado_por_id])


class CalendarioSazonalComercial(db.Model):
    """
    📌 CALENDARIO_SAZONAL_COMERCIAL: Biblioteca de Inteligência Comercial FeedIn
    --------------------------------------------------------------------------------------
    Centraliza datas comemorativas que NÃO são feriados obrigatórios, mas movem o comércio.
    Suporta regras móveis para que o sistema calcule o dia exato ano após ano.
    """
    __tablename__ = 'cal_sazonal_comercial'

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False)  # Ex: "Dia Internacional da Mulher", "Dia dos Pais"
    descricao = db.Column(db.Text, nullable=True)  # Insight de marketing/vendas sugerido pelo FeedIn

    # Armazenamento flexível
    dia = db.Column(db.Integer, nullable=True)  # Preenchido se fixo (Ex: 8)
    mes = db.Column(db.Integer, nullable=True)  # Preenchido se fixo (Ex: 3)
    regra_movel = db.Column(db.String(50),
                            nullable=True)  # Ex: "2_DOMINGO_AGOSTO" (Dia dos Pais), "2_DOMINGO_MAIO" (Dia das Mães)

    # Filtro de Nicho para evitar alertas inúteis
    # Armazena slugs de taxonomia de segmentos (Ex: "petshop,clinica" ou "alimentacao,bar")
    segmentos_alvo = db.Column(db.String(255), nullable=False, default='geral')
    impacto_estimado = db.Column(db.String(20), default='medio')  # 'alto', 'medio', 'baixo'

    def __repr__(self):
        return f"<CalendarioSazonal: {self.nome}>"


"""
==========================================================================================
📌 MÓDULO LOCAL: TABELA AUXILIAR DE RASTREABILIDADE GEOGRÁFICA (SNAPSHOT DE MAPS)
==========================================================================================
Este arquivo resolve a volatilidade geográfica da integração com o Google Maps.
Como a empresa mantém o mesmo 'local_id' de forma vitalícia e apenas edita seus dados 
de endereço e coordenadas dentro da tabela 'locais', este modelo serve para:
  1. Congelamento Histórico: Capturar uma foto das coordenadas antigas (google_place_id)
     antes que o usuário as atualize na tabela principal.
  2. Memória Espacial: Permitir que o sistema saiba quais coordenadas geográficas 
     exatas aquela empresa/local já ocupou em Piracicaba no passado.

Tabelas Gerenciadas:
  - LocalHistoricoGeografico: Log de coordenadas e endereços antigos por 'id_local'.
==========================================================================================
"""
class LocalHistoricoGeografico(db.Model):
    """
    📌 LOCAL_HISTORICO_GEOGRAFICO: O Registro de Mutação Territorial do Ponto
    --------------------------------------------------------------------------------------
    Guarda o histórico de posições no mapa que este 'id_local' já teve.
    Se o estabelecimento se mudou, a coordenada antiga é eternizada aqui antes de ser
    sobrescrevida na tabela mestre 'locais'.
    """
    __tablename__ = 'local_historico_geografico'

    id = db.Column(db.Integer, primary_key=True)

    # 🔗 O ELO VITAL: O local_id que acompanha a empresa e NUNCA muda
    id_local = db.Column(db.Integer, db.ForeignKey('locais.id'), nullable=False)

    # 🗺️ O SNAPSHOT: Dados antigos do Google Maps que seriam perdidos na edição
    google_place_id_antigo = db.Column(db.String(255), nullable=False)
    logradouro_antigo = db.Column(db.String(150), nullable=True)
    numero_antigo = db.Column(db.String(20), nullable=True)
    bairro_antigo = db.Column(db.String(100), nullable=True)
    cep_antigo = db.Column(db.String(10), nullable=True)

    # ⏱️ JANELA TEMPORAL: Período em que a empresa operou nessas coordenadas antigas
    data_registro_antigo = db.Column(db.DateTime, nullable=False, comment="Quando esse endereço foi cadastrado originalmente")
    data_mudanca = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, comment="Momento exato da mudança para as novas coordenadas")

    # Relacionamento para consultas reversas
    local_core = db.relationship('Local', backref=db.backref('historico_coordenadas', lazy=True))


class LocalidadeIBGE(db.Model):
    """
    📌 LOCALIDADE_IBGE: Dicionário territorial de suporte para automações de APIs.
    """
    __tablename__ = 'localidade_ibge'

    id = db.Column(db.Integer, primary_key=True)
    cidade = db.Column(db.String(100), nullable=False)
    estado = db.Column(db.String(2), nullable=False)
    codigo_ibge = db.Column(db.String(10), nullable=False, unique=True)

    def __repr__(self):
        return f"<LocalidadeIBGE: {self.cidade}-{self.estado} ({self.codigo_ibge})>"


class CadastroEfemeride(db.Model):
    """
    📌 CADASTRO_EFEMERIDE: Acervo Histórico, Cultural e Social com Rastreabilidade
    -------------------------------------------------------------------------
    Guarda as efemérides gerais do sistema. Contém dados históricos e o registro
    do usuário responsável pela inserção para fins de auditoria e moderação.
    """
    __tablename__ = 'cadastro_efemeride'

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False)  # Ex: "Dia Nacional do Samba"
    dia = db.Column(db.Integer, nullable=False)  # Ex: 2
    mes = db.Column(db.Integer, nullable=False)  # Ex: 12 (Dezembro)

    # Conteúdo Cultural (Opcionais)
    breve_relato = db.Column(db.Text, nullable=True)
    link_oficial = db.Column(db.String(255), nullable=True)

    # 🛡️ Auditoria e Segurança (Identificação do Responsável)
    usuario_criador_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=False)
    data_cadastro = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # Relacionamento para acessar facilmente os dados do criador se necessário
    criador = db.relationship('Usuario', backref='efemerides_cadastradas')

    def __repr__(self):
        return f"<Efemeride {self.nome} (Criada por User ID {self.usuario_criador_id})>"

class EmpresaRecesso(db.Model):
    """
    📌 EMPRESA_RECESSO: Gestão de Períodos de Fechamento Coletivo
    -------------------------------------------------------------------------
    Permite que a empresa programe intervalos de datas (férias coletivas, reformas, etc)
    em que o estabelecimento estará totalmente inativo.
    """
    __tablename__ = 'empresa_recesso'

    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('ese_empresa.id'), nullable=False)

    # Período do Recesso
    data_inicio = db.Column(db.Date, nullable=False)
    data_fim = db.Column(db.Date, nullable=False)

    motivo = db.Column(db.String(150), nullable=True)  # Ex: "Férias Coletivas" ou "Reforma Geral"

    # 🛡️ Auditoria e Rastreabilidade
    usuario_criador_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=False)
    data_cadastro = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # Relacionamento
    empresa = db.relationship('EseEmpresa', backref='recessos_programados')
    criador = db.relationship('Usuario', backref='recessos_cadastrados')

    def __repr__(self):
        return f"<Recesso Empresa {self.empresa_id}: {self.data_inicio} até {self.data_fim}>"


class EseConfiguracaoAgenda(db.Model):
    """
    📌 ESE_CONFIGURACAO_AGENDA: Painel de Controle de Regras de Negócio do Salão
    --------------------------------------------------------------------------------------
    Centraliza as chaves que definem como a agenda se comporta. Permite que o
    empreendedor ligue ou desligue a obrigatoriedade de pagamento antecipado,
    defina o tempo limite de retenção do PIX e as políticas de cancelamento.
    """
    __tablename__ = 'ese_configuracao_agenda'

    id = db.Column(db.Integer, primary_key=True)
    estabelecimento_id = db.Column(db.Integer, db.ForeignKey('ese_empresa.id'), nullable=False, unique=True)

    # 💳 Configurações de Cobrança Antecipada (A Chave de Transição)
    # False = Agendamento livre (paga no balcão). True = Só confirma se pagar online.
    exigir_pagamento_antecpado = db.Column(db.Boolean, default=False, nullable=False)

    # Quais métodos online ele aceita (Ex: "pix,credito" ou apenas "pix")
    metodos_pagamento_aceitos = db.Column(db.String(50), default='pix')

    # ⏳ Regras de Tempo (Regra do Relógio)
    tempo_bloqueio_reserva_minutos = db.Column(db.Integer, default=10)  # Tempo que o PIX fica aguardando pagamento

    # 🕒 Regras de Cancelamento e Reagendamento
    antecedencia_minima_reagendar_horas = db.Column(db.Integer, default=2)  # Ex: 2 horas antes do procedimento

    def __repr__(self):
        return f"<ConfiguracaoAgenda Empresa ID {self.estabelecimento_id} - Pagamento Obrigatório: {self.exigir_pagamento_antecpado}>"


class EseConviteColaborador(db.Model):
    """
    📌 ESE_CONVITE_COLABORADOR: Tabela Auxiliar de Pré-Cadastro e Alfândega
    --------------------------------------------------------------------------------------
    Guarda o pré-registro feito pelo empreendedor. Quando o colaborador entra na plataforma,
    o sistema cruza a data de nascimento e o hash do CPF para liberar o vínculo.
    """
    __tablename__ = 'ese_convite_colaborador'

    id = db.Column(db.Integer, primary_key=True)
    estabelecimento_id = db.Column(db.Integer, db.ForeignKey('ese_empresa.id'), nullable=False)

    # 🔐 Segurança: Armazenamos apenas o hash SHA-256 do CPF para validação às cegas
    cpf_hash = db.Column(db.String(64), nullable=False, index=True)

    # Validador de consistência (Data de Nascimento)
    data_nascimento = db.Column(db.Date, nullable=False)

    # Dados contratuais iniciais definidos pelo empreendedor
    data_contratacao = db.Column(db.Date, nullable=False)
    nome_proposto = db.Column(db.String(100), nullable=False)  # Apenas para o dono identificar quem convidou

    # Controle de Fluxo
    status = db.Column(db.String(20), default='pendente')  # 'pendente', 'aceito', 'recusado', 'expirado'
    token_convite = db.Column(db.String(100), unique=True,
                              nullable=False)  # Para o link enviado via WhatsApp/E-mail
    criado_em = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    # Relacionamento
    empresa = db.relationship('EseEmpresa', backref=db.backref('convites_colaboradores', lazy=True))

    @staticmethod
    def gerar_hash_cpf(cpf_limpo: str) -> str:
        """Gera o hash SHA-256 do CPF (apenas números) para salvar/comparar de forma segura."""
        cpf_apenas_numeros = "".join(filter(str.isdigit, cpf_limpo))
        return hashlib.sha256(cpf_apenas_numeros.encode('utf-8')).hexdigest()

class EseServicoOferecido(db.Model):
    """
    📌 ESE_SERVICO_OFERECIDO: Catálogo de Serviços Customizados do Estabelecimento
    --------------------------------------------------------------------------------------
    Mapeia os serviços que a empresa de fato oferece a partir da tabela global de Taxonomia.
    Guarda as customizações do empreendedor como descrição própria, tempo de duração padrão
    e a ordenação para a futura montagem da tabela de preços.
    """
    __tablename__ = 'ese_servico_oferecido'
    __table_args__ = {'extend_existing': True}

    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('ese_empresa.id'), nullable=False, index=True)
    taxonomia_id = db.Column(db.Integer, db.ForeignKey('taxonomia.id'), nullable=False)

    # Campo numérico para ordenação/agrupamento na tabela de preços futuramente
    grupo = db.Column(db.Integer, nullable=True)

    # Customização do serviço para o estabelecimento
    descricao_servico = db.Column(db.String(255), nullable=True)
    tempo_duracao = db.Column(db.String(5), nullable=False, default="00:30")  # Formato "HH:mm"

    # Controle e Auditoria
    inserido_por_usuario_id = db.Column(db.Integer, nullable=False)
    data_criacao = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    # Relacionamento virtual para acessar propriedades da Taxonomia (como nome, categoria, etc.)
    servico_taxonomia = db.relationship('Taxonomia', foreign_keys=[taxonomia_id])

    def __repr__(self):
        return f"<EseServicoOferecido {self.id} | Empresa {self.empresa_id} | Taxonomia {self.taxonomia_id}>"


class EseServicoPreco(db.Model):
    """
    📌 ESE_SERVICO_PRECO: Tabela de Preços Vigentes dos Serviços da Empresa
    --------------------------------------------------------------------------------------
    Guarda o valor atual/ativo cobrado por cada serviço oferecido pela empresa.
    O valor é associado à Taxonomia do serviço correspondente para permitir flexibilidade
    e inteligência nas buscas comerciais.
    """
    __tablename__ = 'ese_servico_preco'
    __table_args__ = {'extend_existing': True}

    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('ese_empresa.id'), nullable=False, index=True)
    taxonomia_id = db.Column(db.Integer, db.ForeignKey('taxonomia.id'), nullable=False, index=True)

    # Preço atual armazenado como numérico (Numeric/Float) para precisão decimal
    novo_valor = db.Column(db.Numeric(10, 2), nullable=False, default=0.00)

    # Campo opcional para justificativas ou anotações internas da alteração
    observacao = db.Column(db.String(255), nullable=True)

    # Controle e Auditoria
    alterado_por_usuario_id = db.Column(db.Integer, nullable=False)
    data_alteracao = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                               onupdate=lambda: datetime.now(timezone.utc))

    grupo_id = db.Column(db.String(36), db.ForeignKey('ese_grupo_tabela.id'), nullable=True)

    # Relacionamentos virtuais
    servico_taxonomia = db.relationship('Taxonomia', foreign_keys=[taxonomia_id])

    def __repr__(self):
        return f"<EseServicoPreco Empresa {self.empresa_id} | Taxonomia {self.taxonomia_id} | Valor {self.novo_valor}>"


class EseServicoPrecoHistorico(db.Model):
    """
    📌 ESE_SERVICO_PRECO_HISTORICO: Log de Auditoria e Histórico de Preços
    --------------------------------------------------------------------------------------
    Guarda o rastro histórico de todas as alterações de preços efetuadas no sistema.
    Serve para estudos de inteligência financeira, gráficos de variação de preços e auditoria
    para saber quem, quando e por que um determinado preço foi alterado.
    """
    __tablename__ = 'ese_servico_preco_historico'
    __table_args__ = {'extend_existing': True}

    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('ese_empresa.id'), nullable=False, index=True)
    taxonomia_id = db.Column(db.Integer, db.ForeignKey('taxonomia.id'), nullable=False, index=True)

    # Valor que estava ativo antes da alteração ocorrer
    valor_antigo = db.Column(db.Numeric(10, 2), nullable=False)

    # Observação/justificativa registrada na data daquela alteração específica
    observacao = db.Column(db.String(255), nullable=True)

    # Controle e Auditoria histórica
    alterado_por_usuario_id = db.Column(db.Integer, nullable=False)
    data_alteracao = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    grupo_id = db.Column(db.String(36), db.ForeignKey('ese_grupo_tabela.id'), nullable=True)

    # Relacionamentos virtuais
    servico_taxonomia = db.relationship('Taxonomia', foreign_keys=[taxonomia_id])

    def __repr__(self):
        return f"<EseServicoPrecoHistorico Empresa {self.empresa_id} | Taxonomia {self.taxonomia_id} | Antigo {self.valor_antigo}>"


class EseGrupoTabela(db.Model):
    """
    📌 ESE_GRUPO_TABELA: Agrupamento e Categorização da Vitrine de Preços
    --------------------------------------------------------------------------------------
    Organiza os serviços em grupos específicos (ex: "Barbearia", "Química", "Combos")
    para exibição estruturada na TV da recepção, PWA ou relatórios. Permite ao gestor
    vincular múltiplos serviços a um único grupo visual de forma dinâmica.
    """
    __tablename__ = 'ese_grupo_tabela'
    __table_args__ = {'extend_existing': True}

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    nome_grupo = db.Column(db.String(100), nullable=False)
    empresa_id = db.Column(db.Integer, db.ForeignKey('ese_empresa.id'), nullable=False)

    # Relacionamento para herdar e listar os preços/serviços que pertencem a este grupo
    precos = db.relationship('EseServicoPreco', backref='grupo', lazy=True)


class EseColaboradorServicoHabilidade(db.Model):
    """
    📌 ESE_COLABORADOR_SERVICO_HABILIDADE: Matriz de Capacitação Profissional / Habilidades
    --------------------------------------------------------------------------------------
    Tabela de junção N:N que atrela as habilidades técnicas (serviços executáveis) de um
    colaborador específico aos serviços ativos do catálogo do estabelecimento (EseServicoOferecido).

    Regra de Negócio:
    Desvincula o cargo formal das atribuições operacionais do dia a dia. Permite que dois
    profissionais com o mesmo cargo institucional (ex: "Cabeleireiro") possuam leques de
    serviços prestados distintos na agenda.
    """
    __tablename__ = 'ese_colaborador_servico_habilidade'
    __table_args__ = (
        db.UniqueConstraint('contrato_id', 'servico_oferecido_id', name='uq_ese_colaborador_servico'),
        {'extend_existing': True}
    )

    id = db.Column(db.Integer, primary_key=True)

    # Elo com o contrato de trabalho ativo do colaborador
    contrato_id = db.Column(db.Integer, db.ForeignKey('colaborador_contratos.id'), nullable=False, index=True)

    # Elo com o serviço customizado do estabelecimento
    servico_oferecido_id = db.Column(db.Integer, db.ForeignKey('ese_servico_oferecido.id'), nullable=False, index=True)

    # Registro temporal de quando o colaborador passou a realizar a habilidade
    data_atribuicao = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    # Relacionamentos com os modelos de domínio
    contrato = db.relationship(
        'ColaboradorContrato',
        backref=db.backref('habilidades_servicos', cascade='all, delete-orphan')
    )
    servico_oferecido = db.relationship(
        'EseServicoOferecido',
        backref=db.backref('colaboradores_habilitados', cascade='all, delete-orphan')
    )

    def __repr__(self):
        return f"<EseColaboradorServicoHabilidade Contrato:{self.contrato_id} | Servico:{self.servico_oferecido_id}>"


class EseTipoExcecaoEnum(enum.Enum):
    """
    Enumeração para os tipos de exceção no calendário.
    - EXCECAO: Funcionamento/trabalho em dia/horário fora do padrão (ex: plantão, feriado trabalhado).
    - RECESSO: Ausência total ou parcial de expediente/trabalho (ex: férias, consulta médica, folga, manutenção).
    """
    EXCECAO = 'excecao'
    RECESSO = 'recesso'


class EseOrigemExcecaoEnum(enum.Enum):
    """
    Enumeração da origem da regra de exceção.
    - EMPRESA: Aplica-se à empresa/unidade de atendimento.
    - COLABORADOR: Aplica-se especificamente a um contrato de trabalho/colaborador.
    """
    EMPRESA = 'empresa'
    COLABORADOR = 'colaborador'


class EseExcecaoCalendario(db.Model):
    """
    ===================================================================================
    MODEL: EseExcecaoCalendario
    ===================================================================================
    Descrição:
        Armazena exceções pontuais, recessos totais ou ausências parciais por faixa
        de horário no calendário operacional da Empresa e dos Colaboradores.

    Flexibilidade de Uso:
        1. Recesso Total: data_inicio até data_fim, trabalha=False, considera_horario=False.
        2. Ausência Parcial: data_inicio (1 dia), considera_horario=True,
           com hora_inicio_excecao e hora_fim_excecao definidos (ex: 14:00 às 17:00).
        3. Expediente Especial: trabalha=True, considera_horario=False,
           preenchendo os turnos (inicio_expediente, fim_expediente, etc).

    Módulo:
        Empresa / Escalas / Calendário (ESE)
    ===================================================================================
    """
    __tablename__ = 'ese_excecao_calendario'

    id = db.Column(db.String(36), primary_key=True)

    # Vínculos com Entidades
    empresa_id = db.Column(db.String(36), db.ForeignKey('ese_empresa.id'), nullable=False)
    contrato_id = db.Column(db.String(36), db.ForeignKey('ese_contrato_trabalho.id'), nullable=True)

    # Tipificação e Origem
    origem = db.Column(db.Enum(EseOrigemExcecaoEnum), nullable=False)
    tipo = db.Column(db.Enum(EseTipoExcecaoEnum), nullable=False, default=EseTipoExcecaoEnum.RECESSO)

    # Período de Datas
    data_inicio = db.Column(db.Date, nullable=False)
    data_fim = db.Column(db.Date, nullable=False)

    # --- GRANULARIDADE DE HORÁRIOS ---
    # Flag se a exceção se aplica a uma FAIXA ESPECÍFICA de horas dentro do dia
    considera_horario = db.Column(db.Boolean, default=False, nullable=False)
    hora_inicio_excecao = db.Column(db.Time, nullable=True)  # Ex: 14:00
    hora_fim_excecao = db.Column(db.Time, nullable=True)     # Ex: 17:00

    # Flag se há trabalho no dia/período
    trabalha = db.Column(db.Boolean, default=False, nullable=False)

    # Horários de Expediente Especial (Usado quando trabalha = True e não é apenas bloqueio parcial)
    inicio_expediente = db.Column(db.Time, nullable=True)
    inicio_intervalo = db.Column(db.Time, nullable=True)
    fim_intervalo = db.Column(db.Time, nullable=True)
    fim_expediente = db.Column(db.Time, nullable=True)

    # Contexto e Detalhamento da Ocorrência
    motivo_titulo = db.Column(db.String(100), nullable=False)
    justificativa = db.Column(db.Text, nullable=True)

    # Controle de Auditoria e Status
    ativo = db.Column(db.Boolean, default=True, nullable=False)
    criado_em = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    atualizado_em = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    atualizado_por_id = db.Column(db.String(36), db.ForeignKey('mod_cadastro_cliente.id'), nullable=True)

    def __repr__(self):
        alvo = f"Contrato: {self.contrato_id}" if self.contrato_id else f"Empresa: {self.empresa_id}"
        return f"<EseExcecaoCalendario {self.id} | Origem: {self.origem.value} | {alvo} | Motivo: {self.motivo_titulo}>"