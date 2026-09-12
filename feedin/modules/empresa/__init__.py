import os
from flask import Blueprint

empresa_bp = Blueprint(
    'empresa',
    __name__,
    template_folder='templates',
    static_folder='static',            # <-- OBRIGATÓRIO
    static_url_path='/empresa/static'  # <-- OBRIGATÓRIO para evitar conflitos de rota
)

# 🌍 MAPEAMENTO DINÂMICO DA RAIZ DO MÓDULO
MODULO_RAIZ = os.path.dirname(os.path.abspath(__file__))

# 📌 DIRETÓRIO RAIZ DE UPLOADS DO MÓDULO
# O módulo gerencia a pasta 'uploads' inteira dentro da sua própria static
empresa_bp.UPLOAD_BASE_DIR = os.path.join(MODULO_RAIZ, 'static', 'uploads')

# Garante que a pasta base de uploads exista
os.makedirs(empresa_bp.UPLOAD_BASE_DIR, exist_ok=True)

from . import routes