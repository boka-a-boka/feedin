from feedin import create_app, database
from feedin.models import Usuario
from flask_bcrypt import Bcrypt
import uuid

app = create_app()
bcrypt = Bcrypt(app)

with app.app_context():
    email_teste = "estavalanabalada@gmail.com"
    
    # Remove se já existir para evitar duplicidade
    user_antigo = Usuario.query.filter_by(email=email_teste).first()
    if user_antigo:
        database.session.delete(user_antigo)
        database.session.commit()
        print("Usuário antigo removido.")
    
    # Gera o hash da senha compatível com Flask-Bcrypt
    senha_criptografada = bcrypt.generate_password_hash("123456789").decode('utf-8')
    
    # Cria o novo admin
    novo_admin = Usuario(
        username="admin",
        email=email_teste,
        senha=senha_criptografada,
        nivel_acesso=10,
        active=True,
        fs_uniquifier=str(uuid.uuid4())
    )
    database.session.add(novo_admin)
    database.session.commit()
    print(f"Sucesso! Admin criado com Bcrypt para o e-mail: {email_teste}")
