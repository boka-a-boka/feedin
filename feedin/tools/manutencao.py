from datetime import datetime, timezone
from feedin.models import Epoca, VinculoUsuarioLocal, GrupoSocial


class CaixaFerramentasManutencao:
    """Centraliza todas as rotinas de ajuste de banco de dados."""

    @staticmethod
    def sanear_termos_atualmente(database) -> dict:
        resultado = {"sucesso": False, "vinculos_atualizados": 0, "grupos_atualizados": 0, "mensagem": ""}

        epoca_vigente = Epoca.query.filter_by(eh_vigente=True).first()
        if not epoca_vigente:
            resultado["mensagem"] = "Abortado: Nenhuma época definida como vigente no Core."
            return resultado

        nome_epoca_real = epoca_vigente.nome_exibicao

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

        resultado["sucesso"] = True
        resultado["mensagem"] = f"Sucesso! Aplicado o carimbo '{nome_epoca_real}'."
        return resultado