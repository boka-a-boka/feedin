print("🔥 ATENÇÃO: O ARQUIVO ROUTES.PY DO RATING FOI CARREGADO PELO PYTHON! 🔥")
from flask import request, jsonify
from flask_login import login_required, current_user
from feedin import database as db, csrf
from feedin.modules.rating import rating_bp  # <--- Importa o blueprint existente
from feedin.modules.rating.models import AvaliacaoUniversal


@rating_bp.route('/api/salvar', methods=['POST'])
@csrf.exempt
@login_required
def api_salvar_avaliacao():
    """
    Endpoint universal de salvamento de avaliações, tratado contra campos nulos.
    """
    try:
        data = request.get_json(silent=True) or {}

        modulo_origem = data.get('modulo_origem', 'agenda')
        origem_id = data.get('origem_id')
        colaborador_id = data.get('colaborador_id')
        empresa_id = data.get('empresa_id')
        cargo_id = data.get('cargo_id')
        nota = int(data.get('nota', 5))
        comentario = str(data.get('comentario', '')).strip()

        # Validação do cliente logado
        cliente_uuid = getattr(current_user, 'id', None)
        if not cliente_uuid:
            return jsonify({
                'sucesso': False,
                'erro': 'Sessão expirada ou cliente não identificado.'
            }), 401

        # Tratamento seguro de IDs numéricos/strings opcionais
        try:
            empresa_id = int(empresa_id) if empresa_id else None
        except (ValueError, TypeError):
            empresa_id = None

        if not empresa_id:
            from flask import session
            empresa_id = session.get('empresa_ativa_id') or session.get('empresa_id')

        if not empresa_id:
            return jsonify({
                'sucesso': False,
                'erro': 'ID da empresa não informado.'
            }), 400

        # Converte origem_id e colaborador_id para evitar erros se vierem vazios/nulos
        try:
            origem_id = int(origem_id) if origem_id else None
        except (ValueError, TypeError):
            origem_id = None

        try:
            colaborador_id = int(colaborador_id) if colaborador_id else None
        except (ValueError, TypeError):
            colaborador_id = None

        try:
            cargo_id = int(cargo_id) if cargo_id else None
        except (ValueError, TypeError):
            cargo_id = None

        # Verifica se já existe avaliação para esta mesma origem e cliente
        avaliacao_existente = None
        if origem_id:
            avaliacao_existente = AvaliacaoUniversal.query.filter_by(
                modulo_origem=modulo_origem,
                origem_id=origem_id,
                cliente_id=str(cliente_uuid)
            ).first()

        if avaliacao_existente:
            avaliacao_existente.nota = nota
            avaliacao_existente.comentario = comentario
            if cargo_id:
                avaliacao_existente.cargo_id = cargo_id
            if colaborador_id:
                avaliacao_existente.colaborador_contrato_id = colaborador_id
            if empresa_id:
                avaliacao_existente.empresa_id = empresa_id
        else:
            nova_avaliacao = AvaliacaoUniversal(
                modulo_origem=modulo_origem,
                origem_id=origem_id,
                cliente_id=str(cliente_uuid),
                colaborador_contrato_id=colaborador_id,
                empresa_id=empresa_id,
                cargo_id=cargo_id,
                nota=nota,
                comentario=comentario
            )
            db.session.add(nova_avaliacao)

        db.session.commit()

        return jsonify({
            'sucesso': True,
            'mensagem': 'Avaliação registrada com sucesso!'
        }), 200

    except Exception as e:
        db.session.rollback()
        import traceback
        traceback.print_exc()
        return jsonify({
            'sucesso': False,
            'erro': f'Erro interno no servidor: {str(e)}'
        }), 500