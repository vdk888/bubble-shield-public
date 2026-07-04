"""
gazetteer.py — common French first names, to catch untitled "Prénom Nom".

This is a recall aid, not an exhaustive registry. It lets the NOM recognizer
fire on "Jean Dupont" without a M./Mme cue. Names missed here are exactly
what the reliability bench measures; the optional Presidio/NER layer
(presidio_ext) is the way to push name recall higher when ML is available.
"""
from __future__ import annotations

FRENCH_FIRST_NAMES = {
    # masculins
    "Jean", "Pierre", "Michel", "Alain", "Philippe", "Bernard", "André",
    "Jacques", "Daniel", "Claude", "Christophe", "Patrick", "Nicolas",
    "Thomas", "Julien", "Sébastien", "Stéphane", "Laurent", "David",
    "Olivier", "François", "Guillaume", "Antoine", "Vincent", "Maxime",
    "Alexandre", "Romain", "Mathieu", "Benjamin", "Florian", "Quentin",
    "Hugo", "Lucas", "Théo", "Louis", "Paul", "Arthur", "Gabriel", "Raphaël",
    "Éric", "Eric", "Frédéric", "Pascal", "Thierry", "Didier", "Gérard",
    "Marc", "Henri", "Georges", "Joris", "Rémi", "Rémy", "Damien", "Cédric",
    "Jérôme", "Jérémy", "Gilles", "Xavier", "Fabien", "Bruno", "Yves",
    "Emmanuel", "Adrien", "Clément", "Baptiste", "Victor", "Simon", "Martin",
    "Étienne", "Etienne", "Léo", "Nathan", "Enzo", "Mathis", "Aurélien",
    # féminins
    "Marie", "Nathalie", "Isabelle", "Sylvie", "Catherine", "Martine",
    "Christine", "Françoise", "Monique", "Nicole", "Valérie", "Sophie",
    "Sandrine", "Stéphanie", "Céline", "Julie", "Caroline", "Émilie",
    "Emilie", "Camille", "Laure", "Laura", "Léa", "Manon", "Chloé", "Sarah",
    "Emma", "Inès", "Jade", "Louise", "Alice", "Anne", "Hélène", "Florence",
    "Véronique", "Brigitte", "Dominique", "Patricia", "Aurélie", "Audrey",
    "Élodie", "Elodie", "Mélanie", "Charlotte", "Pauline", "Margaux",
    "Justine", "Clara", "Lucie", "Océane", "Marion", "Amandine", "Delphine",
    "Virginie", "Karine", "Sabrina", "Élise", "Elise", "Agnès", "Claire",
    "Juliette", "Mathilde", "Eléonore", "Éléonore", "Constance", "Adèle",
    # composés fréquents (premier élément)
    "Jean-Pierre", "Jean-Claude", "Jean-Paul", "Jean-Luc", "Jean-Marc",
    "Jean-François", "Jean-Michel", "Marie-Claude", "Marie-Christine",
    "Marie-Hélène", "Anne-Marie", "Pierre-Yves",
    # recall LEAK 2 — bare Title-case "Prénom Nom" mid-sentence: GLiNER scores
    # these below threshold (e.g. "Frédérique Marchand" → 0.21 < 0.30), so the
    # forename gazetteer is what anchors them. These common FR forenames were
    # missing, so the untitled-NOM recognizer never fired. Adding them lifts
    # recall WITHOUT touching precision: the recognizer only fires when the FIRST
    # token is a known forename, so capitalized non-name terms ("Plan Épargne",
    # "Assurance Vie", "Crédit Agricole") are never matched. Screened for
    # collisions against common capitalized FR words — none of these are.
    # masculins
    "Fabrice", "Ludovic", "Grégory", "Jonathan", "Franck", "Cyril", "Mickaël",
    "Michaël", "Guy", "Roger", "Robert", "René", "Marcel", "Lucien", "Raymond",
    "Yannick", "Loïc", "Erwan", "Gwenaël", "Karim", "Rachid", "Mehdi", "Anthony",
    "Kevin", "Fabrice", "Régis", "Serge", "Alexis", "Corentin", "Valentin",
    # féminins
    "Frédérique", "Dominique", "Christelle", "Séverine", "Angélique", "Vanessa",
    "Sonia", "Nadia", "Solène", "Morgane", "Anaïs", "Coralie", "Amélie",
    "Noémie", "Roxane", "Jacqueline", "Colette", "Denise", "Renée", "Suzanne",
    "Simone", "Micheline", "Paulette", "Bernadette", "Yvette", "Ginette",
    "Sabine", "Muriel", "Nadège", "Marion", "Fanny", "Élise",
}
