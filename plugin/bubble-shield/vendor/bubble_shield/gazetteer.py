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
}
