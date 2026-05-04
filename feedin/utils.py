import os, re
import secrets
from PIL import Image
from flask import current_app
from datetime import datetime, timezone

def salvar_imagem(foto):
    """
    Processa a foto: corta em 1:1, redimensiona para 800x800 e salva em .webp
    """

    # 1. Trava de segurança inicial
    if not foto or not hasattr(foto, 'filename') or foto.filename == '':
        return None

    # 2. Gerar nome único com extensão .webp
    codigo = secrets.token_hex(8)
    nome_arquivo = f"perfil_{codigo}.webp"

    # Garante que a pasta existe
    pasta_destino = os.path.join(current_app.root_path, 'static/fotos_perfil')
    if not os.path.exists(pasta_destino):
        os.makedirs(pasta_destino)

    caminho_completo = os.path.join(pasta_destino, nome_arquivo)

    print(f"Tentando salvar a imagem: {foto.filename}")

    try:
        # 3. Abrir a imagem com Pillow
        img = Image.open(foto)

        # 4. Lógica de Crop Central (Garantir o Quadrado Perfeito 1:1)
        largura, altura = img.size
        if largura > altura:
            margem = (largura - altura) / 2
            img = img.crop((margem, 0, largura - margem, altura))
        elif altura > largura:
            margem = (altura - largura) / 2
            img = img.crop((0, margem, largura, altura - margem))

        # 5. Redimensionar para o padrão 800x800
        img = img.resize((800, 800), Image.Resampling.LANCZOS)

        # 6. Salvar em WEBP
        img.save(caminho_completo, "WEBP", quality=85)

        # CORREÇÃO AQUI: Alterado de nome_novo para nome_arquivo
        print(f"DEBUG: Nome gerado pela salvar_imagem: {nome_arquivo}")

        return nome_arquivo

    except Exception as e:
        print(f"Erro ao processar imagem: {e}")
        return None


def tempo_atras_filter(value):
    if not value:
        return ""

    # 1. Forçamos o 'agora' a ser UTC puro (consciente de fuso)
    agora = datetime.now(timezone.utc)

    # 2. Se o valor do banco não tiver fuso (naive), dizemos que ele é UTC
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)

    # 3. Se ele já tiver outro fuso, convertemos para UTC para garantir a conta exata
    else:
        value = value.astimezone(timezone.utc)

    diff = agora - value

    # O restante da sua lógica está correta, mas com uma pequena melhoria na precisão:
    segundos = int(diff.total_seconds())

    if segundos < 0:  # Caso o clock do servidor esteja milissegundos desalinhado
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


# Em utils.py
# Em utils.py

def processar_mudanca_nivel(usuario_alvo, novo_nivel, executor=None):
    """
    Centraliza toda promoção ou rebaixamento.
    Se o nível subir para 10, dispara a verificação de Pioneiro.
    """
    # 1. SEGURANÇA: Verificação de Hierarquia (Se houver executor)
    if executor and executor.nivel_acesso < 999:
        if not (executor.nivel_acesso > usuario_alvo.nivel_acesso and executor.nivel_acesso >= novo_nivel):
            return False, "Você não tem permissão para esta alteração."

    # 2. APLICAÇÃO DO NÍVEL (A mudança física no banco)
    nivel_anterior = usuario_alvo.nivel_acesso
    usuario_alvo.nivel_acesso = novo_nivel

    # 3. GATILHO DE MÉRITO: O momento da "Condecoração"
    # Se o usuário ACABOU de atingir o Nível 10 (ou superior)
    if novo_nivel >= 10 and nivel_anterior < 10:
        # IMPORTANTE: Aqui chamamos o método que criamos no Model Usuario
        # Ele vai olhar o id_indicador e o número de conexões.
        foi_promovido = usuario_alvo.check_pioneiro_status()

        if foi_promovido:
            # Aqui você pode até adicionar um log ou preparar uma mensagem especial
            print(f"Selo de Pioneiro concedido para {usuario_alvo.username}")

    # 4. REGRA PARA EMPREENDEDORES (Pioneiro PJ)
    if novo_nivel == 999:
        # Se for o caso, já marca como pioneiro automaticamente ou aplica validação de CNPJ
        usuario_alvo.is_pioneiro = True

    return True

def obter_signo(data):
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
    if (mes == 12 and dia >= 22) or (mes == 1 and dia <= 19): return ("Capricórnio", "bi-mountains")
    if (mes == 1 and dia >= 20) or (mes == 2 and dia <= 18): return ("Aquário", "bi-droplet")
    return ("Peixes", "bi-water")


def validar_cpf_estrutura(cpf):
    # Remove caracteres não numéricos
    cpf = re.sub(r'\D', '', cpf)

    # Verifica se tem 11 dígitos ou se são todos iguais (ex: 111.111...)
    if len(cpf) != 11 or cpf == cpf[0] * 11:
        return False

    # Cálculo dos dígitos verificadores
    for i in range(9, 11):
        soma = sum(int(cpf[num]) * ((i + 1) - num) for num in range(i))
        digito = (soma * 10 % 11) % 10
        if digito != int(cpf[i]):
            return False
    return True


def proteger_cpf(cpf_limpo):
    # current_app.fernet usa a chave que registramos no __init__
    return current_app.fernet.encrypt(cpf_limpo.encode())

def ler_cpf(cpf_criptografado):
    return current_app.fernet.decrypt(cpf_criptografado).decode()


def salvar_imagem_postagem(foto, usuario_id):
    """
    Processa a foto mantendo a proporção original.
    Converte para WEBP e garante largura máxima de 1200px para qualidade.
    """
    if not foto or not hasattr(foto, 'filename') or foto.filename == '':
        return None

    nome_arquivo = f"post_{usuario_id}_{int(datetime.now().timestamp())}.webp"
    pasta_destino = os.path.join(current_app.root_path, 'static', 'uploads', 'posts')

    if not os.path.exists(pasta_destino):
        os.makedirs(pasta_destino)

    try:
        img = Image.open(foto)

        # Manter proporção original (Aspect Ratio)
        # Redimensionamos apenas se for muito grande para economizar banda
        max_size = (1200, 1200)
        img.thumbnail(max_size, Image.Resampling.LANCZOS)

        img.save(os.path.join(pasta_destino, nome_arquivo), "WEBP", quality=85)
        return nome_arquivo
    except Exception as e:
        print(f"Erro ao processar imagem: {e}")
        return None