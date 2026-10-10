from datetime import datetime, timezone
from feedin import database as db


class AvaliacaoUniversal(db.Model):
    """
    📌 AVALIAÇÃO UNIVERSAL DO ECOSSISTEMA (RATING)
    Tabela centralizada para gerenciar feedbacks de profissionais, serviços e estabelecimentos,
    respeitando os tipos oficiais (como CHAR(36) para identidades e UUIDs de clientes).
    """
    __tablename__ = 'rating_avaliacoes'
    __table_args__ = {'extend_existing': True}

    id = db.Column(db.Integer, primary_key=True)

    # 1. Contexto de Origem da Avaliação (Ex: 'agenda', 'delivery', etc.)
    modulo_origem = db.Column(db.String(30), nullable=False, default='agenda')
    origem_id = db.Column(db.Integer, nullable=True)  # Ex: ID do agendamento de origem

    # 2. Quem Avaliou (Autor): Aponta para o UUID CHAR(36) do ModCadastroCliente
    cliente_id = db.Column(db.String(36), db.ForeignKey('mod_cadastro_cliente.id'), nullable=False, index=True)

    # 3. O Alvo da Avaliação
    colaborador_contrato_id = db.Column(db.Integer, db.ForeignKey('colaborador_contratos.id'), nullable=True,
                                        index=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('ese_empresa.id'), nullable=False, index=True)
    cargo_id = db.Column(db.Integer, db.ForeignKey('cargos.id'), nullable=True, index=True)  # Cargo exercido no momento

    # 4. Conteúdo da Avaliação
    nota = db.Column(db.Integer, nullable=False)  # Ex: 1 a 5 estrelas
    comentario = db.Column(db.Text, nullable=True)

    # 5. Auditoria temporal
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    # Relacionamentos limpos e seguros
    autor_cliente = db.relationship('ModCadastroCliente', backref='avaliacoes_realizadas', lazy=True)
    colaborador_contrato = db.relationship('ColaboradorContrato', backref='avaliacoes_recebidas', lazy=True)
    empresa = db.relationship('EseEmpresa', backref='avaliacoes_estabelecimento', lazy=True)
    cargo = db.relationship('Cargo', backref='avaliacoes_cargo', lazy=True)

    def __repr__(self):
        return f"<AvaliacaoUniversal ID:{self.id} | Nota:{self.nota} | Empresa:{self.empresa_id}>"