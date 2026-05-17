import os, re
import secrets
from PIL import Image, ImageOps
from flask import current_app
from datetime import datetime, timezone

def salvar_imagem(foto):
    if not foto or not hasattr(foto, 'filename') or foto.filename == '':
        return None

    codigo = secrets.token_hex(8)
    nome_arquivo = f"perfil_{codigo}.webp"

    pasta_destino = os.path.join(current_app.root_path, 'static/fotos_perfil')
    if not os.path.exists(pasta_destino):
        os.makedirs(pasta_destino)

    caminho_completo = os.path.join(pasta_destino, nome_arquivo)

    try:
        img = Image.open(foto)

        # --- NOVO: Corrigir orientação EXIF (evita foto deitada) ---
        img = ImageOps.exif_transpose(img)

        # --- NOVO: Converter para RGB (Garante compatibilidade com WebP e remove transparência) ---
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        # 4. Lógica de Crop Central 1:1
        largura, altura = img.size
        if largura > altura:
            margem = (largura - altura) / 2
            img = img.crop((margem, 0, largura - margem, altura))
        elif altura > largura:
            margem = (altura - largura) / 2
            img = img.crop((0, margem, largura, altura - margem))

        # 5. Redimensionar 800x800
        img = img.resize((800, 800), Image.Resampling.LANCZOS)

        # 6. Salvar em WEBP (Quality 85 é o ponto ideal entre peso e qualidade)
        img.save(caminho_completo, "WEBP", quality=85)

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


def processar_mudanca_nivel(usuario_alvo, novo_nivel, executor=None):
    """
    Centraliza toda promoção ou rebaixamento.
    Se o nível subir para 10 durante o Beta, dispara as regras de Pioneiro.
    """
    # 1. SEGURANÇA: Verificação de Hierarquia (Se houver executor)
    if executor and executor.nivel_acesso < 999:
        if not (executor.nivel_acesso > usuario_alvo.nivel_acesso and executor.nivel_acesso >= novo_nivel):
            return False, "Você não tem permissão para esta alteração."

    # 2. APLICAÇÃO DO NÍVEL (A mudança física no banco)
    nivel_anterior = usuario_alvo.nivel_acesso
    usuario_alvo.nivel_acesso = novo_nivel

    # Captura o momento exato e o fim do período Beta configurado
    agora = datetime.now(timezone.utc)
    fim_beta = current_app.config.get('DATA_FIM_BETA')
    is_periodo_beta = fim_beta and agora <= fim_beta

    # 3. GATILHO DE MÉRITO: O momento da "Condecoração" ao atingir Nível 10
    if novo_nivel >= 10 and nivel_anterior < 10:

        # ---------------------------------------------------------------------
        # SITUAÇÃO A: O Usuário veio por indicação (WhatsApp ou Admin)
        # ---------------------------------------------------------------------
        if usuario_alvo.id_indicador:

            # Regra de Convite via WhatsApp (Sempre atualiza o vínculo, mas o selo do padrinho respeita o Beta)
            try:
                from models import Convite
                convite_pendente = Convite.query.filter_by(
                    id_remetente=usuario_alvo.id_indicador,
                    status_onboarding=False
                ).first()

                if convite_pendente:
                    convite_pendente.status_onboarding = True
                    convite_pendente.id_destinatario = usuario_alvo.id

                    # SE ESTIVER NO BETA: Avalia se o PADRINHO ganha o mérito
                    if is_periodo_beta:
                        padrinho = Usuario.query.get(usuario_alvo.id_indicador)

                        if padrinho:
                            # SEU AJUSTE AQUI: A checagem de Pioneiro deve ser feita no PADRINHO,
                            # pois é ele quem está fazendo o "esforço" de indicar pessoas.
                            ganhou_por_esforco_proprio = padrinho.check_pioneiro_status()

                            if ganhou_por_esforco_proprio and not padrinho.is_pioneiro:
                                padrinho.is_pioneiro = True
                                print(
                                    f"🏆 MÉRITO BETA: O padrinho {padrinho.username} atingiu os critérios e virou Pioneiro Vitalício!")
                    else:
                        print(
                            f"Convite validado para estatísticas, mas Padrinho ID {usuario_alvo.id_indicador} não ganha selo (Fim do Beta).")

            except Exception as e:
                print(f"Erro ao processar validação de convite/padrinho: {e}")

        # ---------------------------------------------------------------------
        # SITUAÇÃO B: O Usuário é Orgânico (Não veio por convite)
        # ---------------------------------------------------------------------
        else:
            # Usuários orgânicos apenas sobem de nível, não há padrinho para avaliar.
            if is_periodo_beta:
                print(f"✨ MÉRITO BETA: Usuário orgânico {usuario_alvo.username} promovido a Nível 10.")
            else:
                print(f"🔐 Acesso Liberado: Usuário orgânico {usuario_alvo.username} promovido a Nível 10 (Pós-Beta).")

    # 4. REGRA PARA EMPREENDEDORES (Pioneiro PJ)
    # Geralmente mantida ativa mesmo pós-beta, ou mude para 'if novo_nivel == 999 and is_periodo_beta:' se a regra sumir pós-beta
    if novo_nivel == 999 and is_periodo_beta:
        usuario_alvo.is_pioneiro = True

    return True, "Nível updated com sucesso."


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


def salvar_imagem_capa(foto, usuario_id):
    """
    Processa a foto da capa: converte para WEBP, redimensiona para 1200px de largura
    e otimiza o peso do arquivo.
    """
    if not foto or not hasattr(foto, 'filename') or foto.filename == '':
        return None

    nome_arquivo = f"capa_{usuario_id}_{int(datetime.now().timestamp())}.webp"
    # Certifique-se que o caminho condiz com o seu app.config['UPLOAD_FOLDER_CAPAS']
    pasta_destino = os.path.join(current_app.root_path, 'static', 'uploads', 'capas')

    if not os.path.exists(pasta_destino):
        os.makedirs(pasta_destino)

    try:
        img = Image.open(foto)

        # Converter para RGB (evita erro se a imagem for PNG com transparência)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        # Configuração para Capas: Largura de 1200px é o "sweet spot" para web
        largura_alvo = 1200
        proporcao = largura_alvo / float(img.size[0])
        altura_alvo = int((float(img.size[1]) * float(proporcao)))

        # Redimensiona mantendo a proporção
        img = img.resize((largura_alvo, altura_alvo), Image.Resampling.LANCZOS)

        # Salva em WEBP (Muito mais leve que JPG)
        caminho_completo = os.path.join(pasta_destino, nome_arquivo)
        img.save(caminho_completo, "WEBP", quality=80)

        return nome_arquivo
    except Exception as e:
        print(f"Erro ao processar capa: {e}")
        return None


def obter_lista_negra_usuario(usuario_id):
    """
    Retorna uma lista simples de IDs [2, 45, 88...] contendo todos os usuários
    bloqueados por este usuário ou que bloquearam este usuário.
    """
    try:
        from models import Bloqueios
        from feedin import database
        bloqueados_por_mim = database.session.query(Bloqueios.id_alvo).filter(Bloqueios.id_autor == usuario_id).all()
        me_bloquearam = database.session.query(Bloqueios.id_autor).filter(Bloqueios.id_alvo == usuario_id).all()

        # Converte as tuplas do banco em inteiros limpos em uma única linha
        lista_negra = [id[0] for id in bloqueados_por_mim] + [id[0] for id in me_bloquearam]
        return lista_negra
    except Exception:
        return []