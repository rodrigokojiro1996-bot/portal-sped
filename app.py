"""Ponto de entrada do Portal Fiscal: python app.py [comando].

O código fica no pacote portal/ (veja portal/__init__.py). Os .bat e a tarefa do Windows continuam chamando este arquivo.
"""
from portal import main

if __name__ == "__main__":
    main()
