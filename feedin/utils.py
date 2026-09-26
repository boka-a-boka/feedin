import uuid
import bcrypt
from werkzeug.security import check_password_hash
import os
import re
import secrets
import pytz
import glob
from datetime import datetime, timedelta, time, timezone
from functools import wraps
from unicodedata import normalize

from flask import current_app, redirect, render_template, request, session, url_for, jsonify
from PIL import Image, ImageOps
from werkzeug.utils import secure_filename

from typing import Optional, Union, BinaryIO
from werkzeug.datastructures import FileStorage
from io import BytesIO

# Imports de Modelos do Core
from feedin.models import IdentidadeCivil

# feedin/utils.py

# Configuração Única e Centralizada de Tipos de Imagem
CONFIG_IMAGENS = {
    # Módulo Agenda
    'agenda_avatar': {
        'modulo_alvo': 'agenda',
        'subpasta': 'Avatares',
        'tamanho': (500, 500),
        'qualidade': 85
    },
    # Módulo Empresa
    'empresa_logo': {
        'modulo_alvo': 'empresa',
        'subpasta': 'Uploads/empresas/{empresa_id}',
        'tamanho': (400, 400),
        'qualidade': 90
    },
    'empresa_colaborador': {
        'modulo_alvo': 'empresa',
        'subpasta': 'Uploads/empresas/{empresa_id}/colaboradores',
        'tamanho': (600, 600),
        'qualidade': 85
    },
    'empresa_compliance': {
        'modulo_alvo': 'empresa',
        'subpasta': 'Uploads/empresas/{empresa_id}/compliance',
        'tamanho': (1200, 1200),
        'qualidade': 85
    },
    'empresa_fachada': {
            'modulo_alvo': 'empresa',
            'subpasta': 'Uploads/empresas/{empresa_id}',
            'tamanho': (1200, 800),
            'qualidade': 85
    }
}

MESES_PTBR = {
    1: 'Janeiro', 2: 'Fevereiro', 3: 'Março', 4: 'Abril',
    5: 'Maio', 6: 'Junho', 7: 'Julho', 8: 'Agosto',
    9: 'Setembro', 10: 'Outubro', 11: 'Novembro', 12: 'Dezembro'
}


def formatar_data_extenso(dt):
    """
    Recebe um objeto datetime ou date e retorna
    o mês/ano formatado em PT-BR (ex: 'Agosto de 2026').
    """
    if not dt:
        return ""
    return f"{MESES_PTBR[dt.month]} de {dt.year}"


def validar_unicidade_documento(doc_raw: str, id_local_atual: int = None) -> dict:
    """
    🔍 Função Utilitária Híbrida para validação de unicidade e integridade de CPF/CNPJ.
    Pode ser usada tanto por APIs (retornando dict para jsonify)
    quanto internamente em controllers do Flask.
    """
    # 1. Higienização estrita: extrai apenas dígitos
    doc_limpo = "".join(char for char in doc_raw if char.isdigit()) if doc_raw else ""

    # 2. Validação de tamanho estrutural (11 para CPF, 14 para CNPJ)
    if len(doc_limpo) not in (11, 14):
        return {
            "valido": False,
            "duplicado": False,
            "tipo": None,
            "doc_limpo": doc_limpo,
            "mensagem": "Documento deve conter 11 dígitos (CPF) ou 14 dígitos (CNPJ)."
        }

    tipo_doc = "CPF" if len(doc_limpo) == 11 else "CNPJ"

    # 3. Validação do cálculo dos Dígitos Verificadores (DV)
    if tipo_doc == "CPF" and not validar_cpf(doc_limpo):
        return {
            "valido": False,
            "duplicado": False,
            "tipo": tipo_doc,
            "doc_limpo": doc_limpo,
            "mensagem": "O CPF informado é inválido. Verifique os números digitados."
        }

    if tipo_doc == "CNPJ" and not validar_cnpj(doc_limpo):
        return {
            "valido": False,
            "duplicado": False,
            "tipo": tipo_doc,
            "doc_limpo": doc_limpo,
            "mensagem": "O CNPJ informado é inválido. Verifique os números digitados."
        }

    # 4. Consulta de unicidade na tabela Local
    query = Local.query.filter(Local.documento == doc_limpo)
    if id_local_atual:
        query = query.filter(Local.id != id_local_atual)

    local_existente = query.first()

    if local_existente:
        return {
            "valido": False,
            "duplicado": True,
            "tipo": tipo_doc,
            "doc_limpo": doc_limpo,
            "mensagem": f"Este {tipo_doc} já está vinculado ao estabelecimento '{local_existente.nome}'."
        }

    # Documento válido, consistente e totalmente disponível
    return {
        "valido": True,
        "duplicado": False,
        "tipo": tipo_doc,
        "doc_limpo": doc_limpo,
        "mensagem": f"{tipo_doc} válido e disponível para cadastro."
    }


def tempo_atras_filter(value):
    """Filtro Jinja para exibição de tempo relativo consciente de fuso horário."""
    if not value:
        return ""

    agora = datetime.now(timezone.utc)

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)

    diff = agora - value
    segundos = int(diff.total_seconds())

    if segundos < 0:
        return "agora mesmo"

    if diff.days > 30:
        return value.strftime('%d/%m/%Y')
    elif diff.days > 0:
        return f"{diff.days}d atrás"

    horas = segundos // 3600
    if horas >= 1:
        return f"{horas}h atrás"

    minutos = segundos // 60
    if minutos >= 1:
        return f"{minutos}m atrás"

    return "agora mesmo"


def processar_mudanca_nivel(usuario_alvo, novo_nivel, executor=None):
    """
    Centraliza toda promoção ou rebaixamento de nível de acesso.
    Dispara os gatilhos e méritos de Pioneiro Vitalício durante o Beta.
    """
    if executor and executor.nivel_acesso < 999:
        if not (executor.nivel_acesso > usuario_alvo.nivel_acesso and executor.nivel_acesso >= novo_nivel):
            return False, "Você não tem permissão para esta alteração."

    nivel_anterior = usuario_alvo.nivel_acesso
    usuario_alvo.nivel_acesso = novo_nivel

    agora = datetime.now(timezone.utc)
    fim_beta = current_app.config.get('DATA_FIM_BETA')
    is_periodo_beta = fim_beta and agora <= fim_beta

    if novo_nivel >= 10 and nivel_anterior < 10:
        if usuario_alvo.id_indicador:
            try:
                from models import Convite  # Mantido local por acoplamento dinâmico se necessário
                convite_pendente = Convite.query.filter_by(
                    id_remetente=usuario_alvo.id_indicador,
                    status_onboarding=False
                ).first()

                if convite_pendente:
                    convite_pendente.status_onboarding = True
                    convite_pendente.id_destinatario = usuario_alvo.id

                    if is_periodo_beta:
                        from models import Usuario
                        padrinho = Usuario.query.get(usuario_alvo.id_indicador)

                        if padrinho:
                            ganhou_por_esforco_proprio = padrinho.check_pioneiro_status()
                            if ganhou_por_esforco_proprio and not padrinho.is_pioneiro:
                                padrinho.is_pioneiro = True
                                current_app.logger.info(f"🏆 MÉRITO BETA: Padrinho {padrinho.username} virou Pioneiro Vitalício!")
            except Exception as e:
                current_app.logger.error(f"Erro ao processar validação de convite/padrinho: {str(e)}")
        else:
            if is_periodo_beta:
                current_app.logger.info(f"✨ MÉRITO BETA: Usuário orgânico {usuario_alvo.username} promovido a Nível 10.")

    if novo_nivel == 999 and is_periodo_beta:
        usuario_alvo.is_pioneiro = True

    return True, "Nível atualizado com sucesso."


def obter_signo(data):
    """Calcula o signo do zodíaco e retorna o par (Nome, Ícone do Bootstrap)."""
    if not data:
        return None
    dia, mes = data.day, data.month
    if (mes == 3 and dia >= 21) or (mes == 4 and dia <= 19): return ("Áries", "bi-cloud-lightning")
    if (mes == 4 and dia >= 20) or (mes == 5 and dia <= 20): return ("Touro", "bi-flower1")
    if (mes == 5 and dia >= 21) or (mes == 6 and dia <= 20): return ("Gêmeos", "bi-people")
    if (mes == 6 and dia >= 21) or (mes == 7 and dia <= 22): return ("Câncer", "bi-moon-stars")
    if (mes == 7 and dia >= 23) or (mes == 8 and dia <= 22): return ("Leão", "bi-sun")
    if (mes == 8 and dia >= 23) or (mes == 9 and dia <= 22): return ("Virgem", "bi-leaf")
    if (mes == 9 and dia >= 23) or (mes == 10 and dia <= 22): return ("Libra", "bi-scales")
    if (mes == 10 and dia >= 23) or (mes == 11 and dia <= 21): return ("Escorpião", "bi-bug")
    if (mes == 11 and dia >= 22) or (mes == 12 and dia <= 21): return ("Sagitário", "bi-compass")
    if (mes == 12 and dia >= 22) or (mes == 1 and dia <= 20): return ("Capricórnio", "bi-mountains")
    if (mes == 1 and dia >= 21) or (mes == 2 and dia <= 18): return ("Aquário", "bi-droplet")
    return ("Peixes", "bi-water")


def validar_cpf_estrutura(cpf):
    """Validação algorítmica matemática padrão dos dígitos do CPF."""
    cpf = re.sub(r'\D', '', cpf)

    if len(cpf) != 11 or cpf == cpf[0] * 11:
        return False

    for i in range(9, 11):
        soma = sum(int(cpf[num]) * ((i + 1) - num) for num in range(i))
        digito = (soma * 10 % 11) % 10
        if digito != int(cpf[i]):
            return False
    return True


def proteger_cpf(cpf_limpo):
    """Criptografa a string do CPF usando a chave Fernet registrada do Core."""
    return current_app.fernet.encrypt(cpf_limpo.encode())


def ler_cpf(cpf_criptografado):
    """Descriptografa o CPF para conferência interna ou checagem segura."""
    return current_app.fernet.decrypt(cpf_criptografado).decode()


def obter_lista_negra_usuario(usuario_id):
    """Retorna a lista de IDs de usuários com bloqueio mútuo ativo."""
    try:
        from models import Bloqueios
        from feedin import database
        bloqueados_por_mim = database.session.query(Bloqueios.id_alvo).filter(Bloqueios.id_autor == usuario_id).all()
        me_bloquearam = database.session.query(Bloqueios.id_autor).filter(Bloqueios.id_alvo == usuario_id).all()

        lista_negra = [id[0] for id in bloqueados_por_mim] + [id[0] for id in me_bloquearam]
        return lista_negra
    except Exception as e:
        current_app.logger.error(f"Erro ao obter lista negra de usuários: {str(e)}")
        return []


def verificar_status_local(f):
    """Garante que nenhuma rota processe ações se o Local estiver 'em_construcao'."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        id_local = kwargs.get('id_local') or request.args.get('id_local', type=int)

        if id_local:
            local = Local.query.get(id_local)
            if local and local.status_operacional == 'em_construcao':
                return render_template('locais/perfil_em_construcao.html', local=local)

        return f(*args, **kwargs)

    return decorated_function


def limpar_string(texto):
    """Remove acentos, cedilhas e caracteres especiais de uma string."""
    string_normalizada = normalize('NFKD', texto).encode('ASCII', 'ignore').decode('ASCII')
    return re.sub(r'[^a-zA-Z0-9\s]', '', string_normalizada).lower().strip()


def limpar_mascara(valor):
    """
    Remove qualquer caractere não numérico de uma string (pontos, traços, parênteses, espaços).
    Ideal para higienização de CPF, CNPJ, Telefone e CEP antes da persistência.
    """
    if not valor:
        return None
    # Substitui tudo que NÃO for dígito (\D) por uma string vazia
    return re.sub(r'\D', '', str(valor))


def preparar_e_hashear_cpf(cpf_cru):
    """Higieniza e gera o hash estrutural consumindo a regra da IdentidadeCivil."""
    if not cpf_cru:
        return None, None

    cpf_limpo = re.sub(r'\D', '', str(cpf_cru))
    cpf_hash = IdentidadeCivil.gerar_hash(cpf_limpo)

    return cpf_hash, cpf_limpo


def preparar_entrada_modulo(slug_modulo, rota_destino):
    """Decorator utilitário para registrar o rastro de navegação via HUB do app."""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if request.args.get('origem') == 'hub':
                session['navegacao_via_hub'] = True

            session['modulo_slug_atual'] = slug_modulo
            session['next_url'] = rota_destino

            return f(*args, **kwargs)
        return decorated_function
    return decorator


def salvar_imagem(foto):
    """Processa e otimiza a foto de perfil do usuário (Crop 1:1 centralizado)."""
    if not foto or not hasattr(foto, 'filename') or foto.filename == '':
        return None

    codigo = secrets.token_hex(8)
    nome_arquivo = f"perfil_{codigo}.webp"

    pasta_destino = os.path.join(current_app.root_path, 'static', 'fotos_perfil')
    os.makedirs(pasta_destino, exist_ok=True)

    caminho_completo = os.path.join(pasta_destino, nome_arquivo)

    try:
        img = Image.open(foto)

        # 1. Corrige orientação EXIF (celular)
        img = ImageOps.exif_transpose(img)
        img.load()

        # 2. Converte para RGB tratando canal Alpha (PNG transparente)
        if img.mode in ("RGBA", "LA", "P"):
            fundo = Image.new('RGB', img.size, (255, 255, 255))
            fundo.paste(img, mask=img.split()[3] if img.mode == "RGBA" else None)
            img = fundo
        elif img.mode != "RGB":
            img = img.convert("RGB")

        # 3. Lógica de Crop Central 1:1
        largura, altura = img.size
        if largura > altura:
            margem = (largura - altura) / 2
            img = img.crop((margem, 0, largura - margem, altura))
        elif altura > largura:
            margem = (altura - largura) / 2
            img = img.crop((0, margem, largura, altura - margem))

        # 4. Redimensionar 800x800
        img = img.resize((800, 800), Image.Resampling.LANCZOS)

        # 5. Salvar em WEBP
        img.save(caminho_completo, "WEBP", quality=85)
        return nome_arquivo

    except Exception as e:
        current_app.logger.error(f"Erro ao processar imagem de perfil: {str(e)}")
        return None


# =========================================================================
# 🔐 VALIDADOR POLIMÓRFICO DE HASH DE SENHA
# =========================================================================
def validar_hash_senha(objeto_usuario, senha_digitada: str) -> bool:
    """
    VALIDADOR UNIFICADO DE CREDENCIAIS MULTI-ALGORITMO

    Objetivo:
        Verificar se a senha em texto plano informada pelo usuário corresponde
        ao hash armazenado na entidade (Usuario ou ModCadastroCliente),
        suportando múltiplos algoritmos de hashing (Bcrypt, Werkzeug e Fallback).

    Parâmetros:
        - objeto_usuario: Instância do modelo que possui o atributo de senha
                          ('senha' ou 'senha_hash').
        - senha_digitada (str): Senha em texto limpo fornecida no formulário.

    Premissas e Algoritmos Suportados:
        1. Bcrypt ($2a$, $2b$, $2y$): Valida via biblioteca `bcrypt`.
        2. Werkzeug (pbkdf2, scrypt, argon2): Valida via `check_password_hash`.
        3. Fallback / Plaintext: Comparação direta em ambientes legados/desenvolvimento.

    Retorna:
        - bool: True se a senha for válida, False caso contrário.
    """
    if not objeto_usuario or not senha_digitada:
        return False

        # Extrai o hash do atributo disponível na entidade ('senha' ou 'senha_hash')
    hash_salvo = getattr(objeto_usuario, 'senha', None) or getattr(objeto_usuario, 'senha_hash', None)

    if not hash_salvo:
        return False

    try:
        # Garante que o hash_salvo seja string para análise dos prefixos
        if isinstance(hash_salvo, bytes):
            hash_salvo_str = hash_salvo.decode('utf-8', errors='ignore')
        else:
            hash_salvo_str = str(hash_salvo)

        # 1. Caso 1: Hash no padrão Bcrypt ($2a$, $2b$, $2y$)
        if hash_salvo_str.startswith(('$2a$', '$2b$', '$2y$')):
            # Converte AMBOS estritamente para bytes
            senha_bytes = senha_digitada.encode('utf-8')
            hash_bytes = hash_salvo_str.encode('utf-8') if isinstance(hash_salvo, str) else hash_salvo

            return bcrypt.checkpw(senha_bytes, hash_bytes)

        # 2. Caso 2: Hash no padrão Werkzeug (pbkdf2, scrypt, etc.)
        if ':' in hash_salvo_str or hash_salvo_str.startswith('scrypt:'):
            return check_password_hash(hash_salvo_str, senha_digitada)

        # 3. Caso 3: Comparação legada (Texto Plano)
        return hash_salvo_str == senha_digitada

    except Exception as err:
        if current_app:
            # Removidos emojis para evitar UnicodeEncodeError em terminais Windows (cp1252)
            current_app.logger.error(f"[AUTH] Falha ao verificar hash de senha: {err}")
        return False


# feedin/utils/alfandega.py

def buscar_convites_pendentes_usuario(usuario):
    """
    Verifica se o CPF do usuário logado possui algum convite
    de admissão pendente no banco de dados.
    """
    from feedin.modules.empresa.models import EseConviteColaborador
    cpf_limpo = getattr(usuario, 'cpf', '') or ''
    if not cpf_limpo:
        return []

    cpf_hash = EseConviteColaborador.gerar_hash_cpf(cpf_limpo)
    return EseConviteColaborador.query.filter_by(
        cpf_hash=cpf_hash,
        status='pendente'
    ).all()


# utils.py
# feedin/utils.py
from feedin.models import Local


def validar_cpf(cpf: str) -> bool:
    """
    🧮 Valida os dígitos verificadores (DV) do CPF usando o algoritmo oficial.
    """
    if len(cpf) != 11 or cpf == cpf[0] * 11:
        return False

    # Primeiro dígito verificador
    soma = sum(int(cpf[i]) * (10 - i) for i in range(9))
    resto = (soma * 10) % 11
    digito_1 = resto if resto < 10 else 0
    if int(cpf[9]) != digito_1:
        return False

    # Segundo dígito verificador
    soma = sum(int(cpf[i]) * (11 - i) for i in range(10))
    resto = (soma * 10) % 11
    digito_2 = resto if resto < 10 else 0
    if int(cpf[10]) != digito_2:
        return False

    return True


def validar_cnpj(cnpj: str) -> bool:
    """
    🧮 Valida os dígitos verificadores (DV) do CNPJ usando o algoritmo oficial.
    """
    if len(cnpj) != 14 or cnpj == cnpj[0] * 14:
        return False

    pesos_d1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    pesos_d2 = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]

    # Primeiro dígito verificador
    soma = sum(int(cnpj[i]) * pesos_d1[i] for i in range(12))
    resto = soma % 11
    digito_1 = 0 if resto < 2 else 11 - resto
    if int(cnpj[12]) != digito_1:
        return False

    # Segundo dígito verificador
    soma = sum(int(cnpj[i]) * pesos_d2[i] for i in range(13))
    resto = soma % 11
    digito_2 = 0 if resto < 2 else 11 - resto
    if int(cnpj[13]) != digito_2:
        return False

    return True


def calcular_status_funcionamento(empresa_id, agora=None):
    """
    Calcula o status de funcionamento em tempo real considerando a hierarquia:
    1º EmpresaCalendarioExcecao (Prioridade Máxima)
    2º CadastroFeriado (Feriados Oficiais com abrangencia != 'sazonal')
    3º EseHorarioFuncionamento (Grade Comercial Semanal)
    """
    from feedin.modules.empresa.models import EmpresaCalendarioExcecao, CadastroFeriado, EseHorarioFuncionamento
    if not agora:
        tz = pytz.timezone('America/Sao_Paulo')
        agora = datetime.now(tz)

    data_hoje = agora.date()
    hora_atual = agora.time()

    # Mapeamento: Python (0=Seg ... 6=Dom) -> Banco (0=Dom, 1=Seg ... 6=Sáb)
    dia_semana_python = agora.weekday()
    dia_semana_banco = (dia_semana_python + 1) % 7

    # -------------------------------------------------------------------------
    # 1. CAMADA DE EXCEÇÃO (EmpresaCalendarioExcecao)
    # -------------------------------------------------------------------------
    excecao = EmpresaCalendarioExcecao.query.filter_by(
        empresa_id=empresa_id,
        data_excecao=data_hoje
    ).first()

    if excecao:
        if not excecao.trabalha_no_dia:
            motivo = excecao.feriado_oficial.nome if excecao.feriado_oficial else "Recesso / Fechado"
            return {
                'status': 'fechado',
                'texto': f"Fechado ({motivo})",
                'badge': 'bg-danger text-white',
                'detalhe': 'Sem expediente hoje',
                'pode_agendar': False
            }

        if excecao.horario_abertura_excecao and excecao.horario_fechamento_excecao:
            abertura = excecao.horario_abertura_excecao
            fechamento = excecao.horario_fechamento_excecao

            if abertura <= hora_atual <= fechamento:
                return {
                    'status': 'aberto',
                    'texto': 'Aberto (Horário Especial)',
                    'badge': 'bg-success text-white',
                    'detalhe': f"Fecha às {fechamento.strftime('%H:%M')}",
                    'pode_agendar': True
                }
            elif hora_atual < abertura:
                return {
                    'status': 'fechado',
                    'texto': 'Fechado Agora',
                    'badge': 'bg-danger text-white',
                    'detalhe': f"Abre hoje às {abertura.strftime('%H:%M')}",
                    'pode_agendar': True
                }
            else:
                return {
                    'status': 'fechado',
                    'texto': 'Fechado',
                    'badge': 'bg-secondary text-white',
                    'detalhe': 'Expediente encerrado por hoje',
                    'pode_agendar': True
                }

    # -------------------------------------------------------------------------
    # 2. CAMADA DE FERIADO CIVIL (CadastroFeriado)
    # -------------------------------------------------------------------------
    if not excecao:
        feriado = CadastroFeriado.query.filter(
            CadastroFeriado.data == data_hoje,
            CadastroFeriado.abrangencia != 'sazonal'
        ).first()

        if feriado:
            return {
                'status': 'fechado',
                'texto': f"Fechado ({feriado.nome})",
                'badge': 'bg-warning text-dark',
                'detalhe': 'Feriado oficial - sem expediente',
                'pode_agendar': False
            }

    # -------------------------------------------------------------------------
    # 3. CAMADA DE GRADE SEMANAL NORMAL (EseHorarioFuncionamento)
    # -------------------------------------------------------------------------
    turnos = EseHorarioFuncionamento.query.filter_by(
        empresa_id=empresa_id,
        dia_semana=dia_semana_banco
    ).order_by(EseHorarioFuncionamento.periodo_id).all()

    if not turnos:
        return {
            'status': 'fechado',
            'texto': 'Fechado Hoje',
            'badge': 'bg-secondary text-white',
            'detalhe': 'Não abre neste dia da semana',
            'pode_agendar': False
        }

    for turno in turnos:
        if turno.horario_abertura <= hora_atual <= turno.horario_fechamento:
            return {
                'status': 'aberto',
                'texto': 'Aberto agora',
                'badge': 'bg-success text-white',
                'detalhe': f"Fecha às {turno.horario_fechamento.strftime('%H:%M')}",
                'pode_agendar': True
            }

    if len(turnos) > 1 and turnos[0].horario_fechamento < hora_atual < turnos[1].horario_abertura:
        return {
            'status': 'intervalo',
            'texto': 'Em Pausa / Almoço',
            'badge': 'bg-warning text-dark',
            'detalhe': f"Retorna às {turnos[1].horario_abertura.strftime('%H:%M')}",
            'pode_agendar': True
        }

    primeiro_turno = turnos[0]
    if hora_atual < primeiro_turno.horario_abertura:
        detalhe_str = f"Abre hoje às {primeiro_turno.horario_abertura.strftime('%H:%M')}"
    else:
        detalhe_str = "Expediente encerrado por hoje"

    return {
        'status': 'fechado',
        'texto': 'Fechado agora',
        'badge': 'bg-danger text-white',
        'detalhe': detalhe_str,
        'pode_agendar': True
    }


def _parse_profissional_id(raw_id):
    """Função auxiliar para tratar o parâmetro profissional_id vindo da query string."""
    if raw_id and raw_id != 'qualquer' and str(raw_id).isdigit():
        return int(raw_id)
    return None


def _converter_dia_semana_python_para_ese(dia_python):
    """
    Python weekday(): 0=Segunda, 1=Terça, ..., 5=Sábado, 6=Domingo
    EseHorarioFuncionamento: 0=Domingo, 1=Segunda, ..., 6=Sábado
    """
    return (dia_python + 1) % 7


def converter_duracao_para_minutos(valor_duracao) -> int:
    """Converte valores de duração (str "HH:MM", timedelta, int ou None) para minutos inteiros."""
    if not valor_duracao:
        return 30
    if isinstance(valor_duracao, timedelta):
        return int(valor_duracao.total_seconds() // 60)
    if isinstance(valor_duracao, int):
        return valor_duracao
    if isinstance(valor_duracao, str):
        partes = valor_duracao.strip().split(':')
        try:
            if len(partes) >= 2:
                return (int(partes[0]) * 60) + int(partes[1])
            return int(partes[0])
        except ValueError:
            return 30
    return 30


def salvar_imagem_modulo(
        arquivo: Union[FileStorage, BytesIO],
        tipo_midia: str,
        identificador: Union[int, str],
        empresa_id: Optional[Union[int, str]] = None
) -> Optional[str]:
    """
    Processador Unificado de Imagens Modular.

    - Converte rigorosamente TODAS as imagens para .webp
    - Respeita a estrutura isolada de pastas de cada Blueprint
    - Retorna o caminho relativo apropriado para persistência no Banco de Dados
    """
    if not arquivo or not getattr(arquivo, 'filename', None):
        return None

    config = CONFIG_IMAGENS.get(tipo_midia)
    if not config:
        current_app.logger.error(f"[UPLOAD] Tipo de mídia inválido: {tipo_midia}")
        return None

    try:
        # 1. Normalização do Stream
        if hasattr(arquivo, 'seek'):
            arquivo.seek(0)

        img = Image.open(arquivo)
        img = ImageOps.exif_transpose(img)

        # 2. Tratamento Inteligente de Transparência
        if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
            # Preserva o canal Alpha para manter fundos vazados/transparentes no WebP
            img = img.convert('RGBA')
        elif img.mode != 'RGB':
            # Mantém em RGB apenas imagens opacas (JPEGs, etc.) para otimizar tamanho
            img = img.convert('RGB')

        # 3. Otimização de Tamanho
        img.thumbnail(config['tamanho'], Image.Resampling.LANCZOS)

        # 4. Resolução da Pasta Física por Blueprint
        modulo_alvo = config['modulo_alvo']
        subpasta_template = config['subpasta']

        # Formata subpasta dinamicamente se houver empresa_id
        subpasta_relativa = subpasta_template.format(empresa_id=str(empresa_id or ''))

        # Monta caminho físico do módulo correspondente
        caminho_base_modulo = os.path.join(
            current_app.root_path,
            'modules',
            modulo_alvo,
            'static'
        )

        diretorio_destino = os.path.normpath(os.path.join(caminho_base_modulo, subpasta_relativa))
        os.makedirs(diretorio_destino, exist_ok=True)

        # 5. Nome do Arquivo WebP
        codigo_hash = uuid.uuid4().hex[:8]
        nome_arquivo = f"{tipo_midia}_{identificador}_{codigo_hash}.webp"
        caminho_completo = os.path.join(diretorio_destino, nome_arquivo)

        # 6. Salvar em WebP (preserva RGBA se for transparente, ou RGB se for opaco)
        img.save(caminho_completo, 'WEBP', quality=config['qualidade'], optimize=True)

        # 7. Limpeza de Versões Antigas do Mesmo Item
        padrao_busca = os.path.join(diretorio_destino, f"{tipo_midia}_{identificador}_*.webp")
        for arq_antigo in glob.glob(padrao_busca):
            if os.path.abspath(arq_antigo) != os.path.abspath(caminho_completo):
                try:
                    os.remove(arq_antigo)
                except OSError:
                    pass

        # Retorna o caminho relativo a partir do 'static' do módulo
        # Exemplo: "Uploads/empresas/12/empresa_logo_12_a1b2c3d4.webp"
        caminho_relativo_db = os.path.join(subpasta_relativa, nome_arquivo).replace('\\', '/')
        return caminho_relativo_db

    except Exception as e:
        current_app.logger.exception(f"[ERRO UPLOAD] Falha ao salvar {tipo_midia} (ID {identificador}): {e}")
        return None


from flask import Blueprint, current_app, send_from_directory, url_for
import os

# 1. Instancia o Blueprint
media_bp = Blueprint('media_global', __name__)


# 2. Define a rota de entrega de arquivos dos módulos
@media_bp.route('/media/<modulo>/<path:filename>')
def servir_midia_modulo(modulo, filename):
    """
    Entrega imagens salvas pela salvar_imagem_modulo()
    buscando na pasta 'static' do módulo correspondente.
    """
    pasta_modulo = os.path.join(current_app.root_path, 'modules', modulo, 'static')
    caminho_arquivo = os.path.join(pasta_modulo, filename)

    # Se o arquivo não existir fisicamente, evita erro 500 e entrega o avatar padrão
    if not os.path.exists(caminho_arquivo):
        return send_from_directory(
            os.path.join(current_app.root_path, 'static', 'img'),
            'avatar-default.png'
        )

    return send_from_directory(pasta_modulo, filename)


from flask import url_for

def resolver_url_midia(caminho_arquivo: str, modulo: str, fallback_filename: str = None) -> str:
    """
    Padroniza a resolução de URLs de mídia para o formato WebP e rotas centralizadas (/media/).
    """
    if not caminho_arquivo:
        if fallback_filename:
            return url_for('static', filename=f'img/{fallback_filename}')
        return None

    # Se já for link externo completo (S3, CDN, URL absoluta)
    if caminho_arquivo.startswith(('http://', 'https://')):
        return caminho_arquivo

    caminho_limpo = caminho_arquivo.lstrip('/')

    try:
        return url_for('media_global.servir_midia_modulo', modulo=modulo, filename=caminho_limpo)
    except Exception:
        return f"/media/{modulo}/{caminho_limpo}"