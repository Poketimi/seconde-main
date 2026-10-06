"""Un collecteur de routes, pour découper l'app sans renommer les endpoints.

Des blueprints Flask auraient préfixé chaque endpoint — `url_for('index')`
serait devenu `url_for('searches.index')` — soit une réécriture de tous les
gabarits, et `request.endpoint` cassé dans la navigation, pour zéro gain.

Ici chaque module garde `@app.route(...)` mot pour mot ; `app` est simplement
ce collecteur, et `apply()` rejoue tout sur la vraie application. Le nom de
l'endpoint reste celui de la fonction.
"""

class Router:
    def __init__(self):
        self.rules = []

    def route(self, rule, **opts):
        def deco(fn):
            self.rules.append((rule, fn, opts))
            return fn
        return deco

    def get(self, rule, **opts):
        return self.route(rule, methods=["GET"], **opts)

    def post(self, rule, **opts):
        return self.route(rule, methods=["POST"], **opts)

    def apply(self, app):
        for rule, fn, opts in self.rules:
            app.add_url_rule(rule, fn.__name__, fn, **opts)
        return len(self.rules)
