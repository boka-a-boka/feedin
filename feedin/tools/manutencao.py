from datetime import datetime, timezone
from sqlalchemy import inspect, text
from feedin.models import Epoca, VinculoUsuarioLocal, GrupoSocial


class CaixaFerramentasManutencao:
    """Centraliza todas as rotinas de manutenção, saneamento e migração de banco de dados."""

    @staticmethod
    def sanear_termos_atualmente(database) -> dict:
        """
        Substitui referências dinâmicas 'Atualmente' pelo nome da Época vigente
        nas tabelas VinculoUsuarioLocal e GrupoSocial.
        """
        resultado = {"sucesso": False, "vinculos_atualizados": 0, "grupos_atualizados": 0, "mensagem": ""}

        epoca_vigente = Epoca.query.filter_by(eh_vigente=True).first()
        if not epoca_vigente:
            resultado["mensagem"] = "Abortado: Nenhuma época definida como vigente no Core."
            return resultado

        nome_epoca_real = epoca_vigente.nome_exibicao

        try:
            # Corrige VinculoUsuarioLocal
            vinculos_legados = VinculoUsuarioLocal.query.filter(
                VinculoUsuarioLocal.experiencia.ilike('%Atualmente%')
            ).all()
            for vinculo in vinculos_legados:
                vinculo.experiencia = nome_epoca_real
                resultado["vinculos_atualizados"] += 1

            # Corrige GrupoSocial
            grupos_legados = GrupoSocial.query.filter(
                GrupoSocial.periodo_referencia.ilike('%Atualmente%')
            ).all()
            for grupo in grupos_legados:
                grupo.periodo_referencia = nome_epoca_real
                resultado["grupos_atualizados"] += 1

            database.session.commit()
            resultado["sucesso"] = True
            resultado["mensagem"] = f"Sucesso! Aplicado o carimbo '{nome_epoca_real}'."

        except Exception as e:
            database.session.rollback()
            resultado["sucesso"] = False
            resultado["mensagem"] = f"Erro ao sanear termos 'Atualmente': {str(e)}"

        return resultado

    @staticmethod
    def sanear_estrutura_mod_cadastro_cliente(database) -> dict:
        """
        Garante a alteração física da tabela 'mod_cadastro_cliente' no VPS:
        - Adiciona a coluna 'genero' (INTEGER, NULL) se não existir.
        - Adiciona a coluna 'estado_civil' (INTEGER, NULL) e sua FK para 'estadocivil(id)' se não existir.
        """
        resultado = {"sucesso": False, "colunas_adicionadas": [], "mensagem": ""}

        try:
            inspector = inspect(database.engine)
            colunas_existentes = [col['name'] for col in inspector.get_columns('mod_cadastro_cliente')]

            # 1. Adiciona coluna genero se necessário
            if 'genero' not in colunas_existentes:
                database.session.execute(text(
                    "ALTER TABLE mod_cadastro_cliente ADD COLUMN genero INTEGER NULL;"
                ))
                resultado["colunas_adicionadas"].append("genero")

            # 2. Adiciona coluna estado_civil se necessário
            if 'estado_civil' not in colunas_existentes:
                database.session.execute(text(
                    "ALTER TABLE mod_cadastro_cliente ADD COLUMN estado_civil INTEGER NULL "
                    "REFERENCES estadocivil(id);"
                ))
                resultado["colunas_adicionadas"].append("estado_civil")

            database.session.commit()
            resultado["sucesso"] = True

            if resultado["colunas_adicionadas"]:
                cols_str = ", ".join(resultado["colunas_adicionadas"])
                resultado["mensagem"] = f"Sucesso! Estrutura alterada no VPS. Colunas criadas: {cols_str}."
            else:
                resultado["mensagem"] = "Tabela 'mod_cadastro_cliente' já possui todas as colunas necessárias."

        except Exception as e:
            database.session.rollback()
            resultado["sucesso"] = False
            resultado["mensagem"] = f"Erro ao alterar estrutura da tabela mod_cadastro_cliente: {str(e)}"

        return resultado

    @classmethod
    def executar_todas_manutencoes(cls, database) -> dict:
        """Executa o pacote completo de rotinas de manutenção para atualização no VPS."""
        res_termos = cls.sanear_termos_atualmente(database)
        res_estrutura = cls.sanear_estrutura_mod_cadastro_cliente(database)

        return {
            "saneamento_termos": res_termos,
            "migracao_estrutura": res_estrutura,
            "executado_em": datetime.now(timezone.utc).isoformat()
        }