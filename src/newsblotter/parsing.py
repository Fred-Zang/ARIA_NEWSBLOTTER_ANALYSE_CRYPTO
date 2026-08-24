"""Parsing défensif des flux RSS téléchargés depuis le Web."""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

_FORBIDDEN_XML_DECLARATIONS = re.compile(r"<!\s*(DOCTYPE|ENTITY)\b", re.IGNORECASE)


def parse_rss_xml(xml_content: str) -> BeautifulSoup:
    """Parse un flux RSS après rejet des déclarations DTD/ENTITY externes."""
    if _FORBIDDEN_XML_DECLARATIONS.search(xml_content):
        raise ValueError("Flux XML contenant une déclaration DTD/ENTITY interdite")
    return BeautifulSoup(xml_content, "lxml-xml")
