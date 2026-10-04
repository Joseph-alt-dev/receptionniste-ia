"""Composition d'une adresse lisible à partir des champs structurés de Salon."""


def composer_adresse(
    numero_et_rue: str | None,
    complement: str | None,
    code_postal: str | None,
    ville: str | None,
    pays: str | None,
) -> str:
    ligne1 = ", ".join(p for p in [numero_et_rue, complement] if p)
    ligne2 = " ".join(p for p in [code_postal, ville] if p)
    pays_affiche = pays if pays and pays.strip().lower() != "france" else None
    return ", ".join(p for p in [ligne1, ligne2, pays_affiche] if p)
