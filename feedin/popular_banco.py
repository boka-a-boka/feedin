import sys
import os

# Força o Python a olhar primeiro a raiz do seu projeto
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__) + '/..'))

# Agora ele não vai mais confundir com a venv!
from feedin import app
from feedin.models import Postagem, database

with app.app_context():
    print("Iniciando a correção cirúrgica dos IDs...")

    regras = [
        {"ids": range(1, 13), "local": 362},
        {"ids": range(13, 19), "local": 500},
        {"ids": range(19, 29), "local": 668},
        {"ids": range(31, 68), "local": 669},
        {"ids": range(68, 81), "local": 672},
        {"ids": range(81, 90), "local": 670},
        {"ids": range(90, 96), "local": 474},
        {"ids": [96], "local": 670},
        {"ids": range(97, 107), "local": 669},
        {"ids": range(107, 124), "local": 671}
    ]

    linhas_alteradas = 0
    for regra in regras:
        postagens = Postagem.query.filter(Postagem.id.in_(list(regra["ids"]))).all()
        for post in postagens:
            post.id_local = regra["local"]
            linhas_alteradas += 1

    try:
        database.session.commit()
        print(f"Sucesso total! {linhas_alteradas} postagens foram devidamente carimbadas e salvas.")
    except Exception as e:
        database.session.rollback()
        print(f"Erro crítico ao tentar commitar via Python: {e}")