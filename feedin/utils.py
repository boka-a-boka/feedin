import os
import re
import secrets
from datetime import datetime, timezone
from functools import wraps
from unicodedata import normalize

from flask import current_app, redirect, render_template, request, session, url_for, jsonify
from PIL import Image, ImageOps
from werkzeug.utils import secure_filename

# Imports de Modelos do Core
from feedin.models import IdentidadeCivil, Local

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


def salvar_imagem_postagem(foto, usuario_id):
    """Processa fotos de postagens da linha do tempo mantendo a proporção original."""
    if not foto or not hasattr(foto, 'filename') or foto.filename == '':
        return None

    nome_arquivo = f"post_{usuario_id}_{int(datetime.now().timestamp())}.webp"
    pasta_destino = os.path.join(current_app.root_path, 'static', 'uploads', 'posts')
    os.makedirs(pasta_destino, exist_ok=True)

    try:
        img = Image.open(foto)

        # 1. Corrige orientação EXIF para fotos do feed não deitarem
        img = ImageOps.exif_transpose(img)

        # 2. Converte para RGB tratando transparências
        if img.mode in ("RGBA", "LA", "P"):
            fundo = Image.new('RGB', img.size, (255, 255, 255))
            fundo.paste(img, mask=img.split()[3] if img.mode == "RGBA" else None)
            img = fundo
        elif img.mode != "RGB":
            img = img.convert("RGB")

        # 3. Mantém proporção original impondo teto máximo de segurança
        max_size = (1200, 1200)
        img.thumbnail(max_size, Image.Resampling.LANCZOS)

        img.save(os.path.join(pasta_destino, nome_arquivo), "WEBP", quality=85)
        return nome_arquivo
    except Exception as e:
        current_app.logger.error(f"Erro ao processar imagem de postagem: {str(e)}")
        return None


def salvar_imagem_capa(foto, usuario_id):
    """Otimiza e redimensiona a imagem de capa do perfil para o teto de 1200px de largura."""
    if not foto or not hasattr(foto, 'filename') or foto.filename == '':
        return None

    nome_arquivo = f"capa_{usuario_id}_{int(datetime.now().timestamp())}.webp"
    pasta_destino = os.path.join(current_app.root_path, 'static', 'uploads', 'capas')
    os.makedirs(pasta_destino, exist_ok=True)

    try:
        img = Image.open(foto)

        # 1. Corrige orientação EXIF
        img = ImageOps.exif_transpose(img)

        # 2. Converte para RGB tratando transparências
        if img.mode in ("RGBA", "LA", "P"):
            fundo = Image.new('RGB', img.size, (255, 255, 255))
            fundo.paste(img, mask=img.split()[3] if img.mode == "RGBA" else None)
            img = fundo
        elif img.mode != "RGB":
            img = img.convert("RGB")

        # 3. Configuração para Capas: Proporcional com largura alvo de 1200px
        largura_alvo = 1200
        proporcao = largura_alvo / float(img.size[0])
        altura_alvo = int((float(img.size[1]) * float(proporcao)))

        img = img.resize((largura_alvo, altura_alvo), Image.Resampling.LANCZOS)

        caminho_completo = os.path.join(pasta_destino, nome_arquivo)
        img.save(caminho_completo, "WEBP", quality=80)
        return nome_arquivo
    except Exception as e:
        current_app.logger.error(f"Erro ao processar imagem de capa: {str(e)}")
        return None


def salvar_imagem_anuncio(foto, local_id):
    """Processa e otimiza a imagem/flyer de publicidade de estabelecimentos locais."""
    if not foto or not hasattr(foto, 'filename') or foto.filename == '':
        return None

    nome_arquivo = f"anuncio_{local_id}_{int(datetime.now().timestamp())}.webp"
    pasta_destino = os.path.join(current_app.root_path, 'static', 'uploads', 'anuncios')
    os.makedirs(pasta_destino, exist_ok=True)

    caminho_completo = os.path.join(pasta_destino, nome_arquivo)

    try:
        img = Image.open(foto)

        # 1. Corrige orientação EXIF
        img = ImageOps.exif_transpose(img)

        # 2. Converte para RGB tratando transparências
        if img.mode in ("RGBA", "LA", "P"):
            fundo = Image.new('RGB', img.size, (255, 255, 255))
            fundo.paste(img, mask=img.split()[3] if img.mode == "RGBA" else None)
            img = fundo
        elif img.mode != "RGB":
            img = img.convert("RGB")

        # 3. Redimensionamento Proporcional de Segurança
        LIMITE_MAXIMO = 1200
        largura, altura = img.size

        if largura > LIMITE_MAXIMO or altura > LIMITE_MAXIMO:
            if largura > altura:
                nova_largura = LIMITE_MAXIMO
                nova_altura = int((altura * LIMITE_MAXIMO) / largura)
            else:
                nova_altura = LIMITE_MAXIMO
                nova_largura = int((largura * LIMITE_MAXIMO) / altura)

            img = img.resize((nova_largura, nova_altura), Image.Resampling.LANCZOS)

        img.save(caminho_completo, "WEBP", quality=85)
        return nome_arquivo
    except Exception as e:
        current_app.logger.error(f"Erro ao processar imagem do anúncio: {str(e)}")
        return None
