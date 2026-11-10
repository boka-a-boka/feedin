#!/bin/bash
# 🚀 Script de Deploy Automatizado - FeedIn
# Interrompe a execução caso ocorra algum erro
set -e

# Configurações (Ajuste se o seu caminho for diferente)
PROJECT_DIR="/var/www/feedin"  # Exemplo: /home/usuario/feedin ou /var/www/...
SERVICE_NAME="feedin"         # Nome do seu serviço no systemd (ex: feedin, gunicorn)

echo "🔄 [1/5] Acessando o diretório do projeto..."
cd "$PROJECT_DIR"

echo "📥 [2/5] Puxando as últimas alterações do repositório Git..."
git pull origin main

echo "🧹 [3/5] Limpando o cache do Python e templates compilados..."
find . -type f -name "*.pyc" -delete
find . -type d -name "__pycache__" -exec rm -r {} + 2>/dev/null || true

echo "📦 [4/5] Atualizando dependências do ambiente virtual (se houver)..."
if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
    pip install -r requirements.txt --quiet
fi

echo "🔄 [5/5] Reiniciando o serviço Gunicorn ($SERVICE_NAME)..."
sudo systemctl restart "$SERVICE_NAME"

echo "✨ Deploy concluído com sucesso na VPS!"
