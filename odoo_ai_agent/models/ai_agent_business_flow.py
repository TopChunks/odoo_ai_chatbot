"""The Business Flow menu: each process of this database as its documents, their stages, the actions that move
them and who may run each action, plus the custom rules found in the business-flow index.

Built with the context update from the database itself (status fields, stage records, form view buttons, access
rights). No AI provider is called, so building it costs no tokens. A text copy of each flow goes into the index,
so chat questions such as "what happens after the quotation?" find it.
"""

import ast
import json
import logging

from markupsafe import Markup

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

# (flow, the document of its own app, its documents in order). A document is (model, what users call it).
# A flow is shown only when its own app is installed (no "Purchase" flow made of invoices alone); other
# documents of the chain are skipped when their app is missing (Sales without CRM starts at the quotation).
STANDARD_FLOWS = [
    ("Sales", "sale.order", [("crm.lead", "Opportunity"), ("sale.order", "Quotation / Sales Order"),
                             ("stock.picking", "Delivery"), ("account.move", "Customer Invoice"),
                             ("account.payment", "Customer Payment")]),
    ("Purchase", "purchase.order", [("purchase.order", "Request for Quotation / Purchase Order"),
                                    ("stock.picking", "Receipt"), ("account.move", "Vendor Bill"),
                                    ("account.payment", "Vendor Payment")]),
    ("Manufacturing", "mrp.production", [("mrp.production", "Manufacturing Order")]),
    ("Inventory", "stock.picking", [("stock.picking", "Transfer")]),
    ("Invoicing", "account.move", [("account.move", "Invoice / Bill"), ("account.payment", "Payment")]),
    ("Projects", "project.task", [("project.task", "Task")]),
    ("Helpdesk", "helpdesk.ticket", [("helpdesk.ticket", "Ticket")]),
    ("Repairs", "repair.order", [("repair.order", "Repair Order")]),
    ("Time Off", "hr.leave", [("hr.leave", "Time Off Request")]),
    ("Expenses", "hr.expense", [("hr.expense", "Expense")]),
]
SKIP_ACTIONS = {"Print", "Preview"}  # on every document, not a step of the flow
MAX_STAGES = 12
MAX_ACTIONS = 15
MAX_RULES = 8


OPPOSITE = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.In: ast.NotIn, ast.NotIn: ast.In,
            ast.Gt: ast.LtE, ast.LtE: ast.Gt, ast.GtE: ast.Lt, ast.Lt: ast.GtE}
OPERATOR_WORDS = {ast.Eq: "is", ast.NotEq: "is not", ast.In: "is one of", ast.NotIn: "is not one of",
                  ast.Gt: "is more than", ast.GtE: "is at least", ast.Lt: "is less than", ast.LtE: "is at most"}


def opposite(first, second):
    """Whether two condition words are each other's opposite ("Locked" / "not Locked", "is set" / "is empty")."""
    pairs = [("not ", ""), (" is empty", " is set"), (" is not ", " is ")]
    for one, other in pairs:
        if first.replace(one, other, 1) == second or second.replace(one, other, 1) == first:
            return first != second
    return False


def merge_opposites(conditions):
    """One action drawn as two buttons that differ in one opposite condition (a highlighted and a plain "Send")
    is available whatever that condition is: drop it. [(stage keys, words)] in, the same out."""
    conditions = list(conditions)
    merged = True
    while merged:
        merged = False
        for i, (keys_a, words_a) in enumerate(conditions):
            for j in range(i + 1, len(conditions)):
                keys_b, words_b = conditions[j]
                parts_a, parts_b = words_a.split(" and "), words_b.split(" and ")
                if (set(keys_a) != set(keys_b) or " or " in words_a + words_b or len(parts_a) != len(parts_b)):
                    continue
                only_a = [p for p in parts_a if p not in parts_b]
                only_b = [p for p in parts_b if p not in parts_a]
                if len(only_a) == len(only_b) == 1 and opposite(only_a[0], only_b[0]):
                    conditions[i] = (keys_a, " and ".join(p for p in parts_a if p in parts_b))
                    del conditions[j]
                    merged = True
                    break
            if merged:
                break
    return conditions


def literal(node):
    """The constant, or list of constants, a rule compares with; None if it is anything else."""
    try:
        value = ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError):
        return None
    return value if isinstance(value, (str, int, float, bool, list, tuple)) else None


def simplify(node, state):
    """The rule with the stage filled in: parts about the stage become True/False and drop out."""
    known = evaluate(node, state)
    if known is not None:
        return ast.Constant(known)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return ast.UnaryOp(ast.Not(), simplify(node.operand, state))
    if isinstance(node, ast.BoolOp):
        decisive = isinstance(node.op, ast.Or)
        values = [simplify(value, state) for value in node.values]
        values = [v for v in values if not (isinstance(v, ast.Constant) and bool(v.value) != decisive)]
        return values[0] if len(values) == 1 else ast.BoolOp(node.op, values) if values else ast.Constant(not decisive)
    return node


def evaluate(node, state):
    """A button's `invisible` rule in one stage: True (hidden), False (shown) or None (depends on other fields)."""
    if isinstance(node, ast.Constant):
        return bool(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        value = evaluate(node.operand, state)
        return None if value is None else not value
    if isinstance(node, ast.BoolOp):
        values = [evaluate(value, state) for value in node.values]
        decisive = isinstance(node.op, ast.Or)  # True decides an "or", False decides an "and"
        if decisive in values:
            return decisive
        return None if None in values else not decisive
    if isinstance(node, ast.Name) and node.id == "state":
        return bool(state)
    if (isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.left, ast.Name)
            and node.left.id == "state" and literal(node.comparators[0]) is not None):
        value, operator = literal(node.comparators[0]), node.ops[0]
        values = value if isinstance(value, (list, tuple)) else [value]
        if isinstance(operator, (ast.Eq, ast.In)):
            return state in values
        if isinstance(operator, (ast.NotEq, ast.NotIn)):
            return state not in values
    return None


class AiAgentBusinessFlow(models.Model):
    _name = "ai_agent.business.flow"
    _description = "AI Agent Business Flow"
    _order = "sequence, name"

    name = fields.Char(required=True, readonly=True)
    sequence = fields.Integer(readonly=True)
    documents = fields.Char(readonly=True, help="The documents this flow passes through, in order.")
    stage_count = fields.Integer(string="Stages", readonly=True)
    action_count = fields.Integer(string="Actions", readonly=True)
    rule_count = fields.Integer(string="Custom rules", readonly=True)
    # Built here from escaped values only (see _diagram), never from user input.
    diagram = fields.Html(readonly=True, sanitize=False)

    # ------------------------------------------------------------------
    # Reading one document type
    # ------------------------------------------------------------------
    @api.model
    def _stages(self, model):
        """[(key, label)] of the document's stages, and whether they are the `state` field (keys usable in
        button conditions) or stage records."""
        Model = self.env[model]
        stage = Model._fields.get("stage_id")
        if stage and stage.type == "many2one" and "sequence" in self.env[stage.comodel_name]._fields:
            Stage = self.env[stage.comodel_name].sudo()
            shared = [("user_id", "=", False)] if "user_id" in Stage._fields else []  # not personal task stages
            names = Stage.search(shared, order="sequence, id", limit=40).mapped("name")
            return [(name, name) for name in dict.fromkeys(names)][:MAX_STAGES], False
        state = Model._fields.get("state")
        if state and state.type == "selection":
            return list(state._description_selection(self.env))[:MAX_STAGES], True
        return [], False

    @api.model
    def _available(self, model, invisible, stages, by_state):
        """When a button with this `invisible` rule can be pressed: (stage keys, the other conditions in words).

        The rule is read with Python's parser and evaluated per stage with three values (hidden, shown, depends on
        other fields), so a stage is listed unless the rule surely hides the button there. Conditions on other
        fields are turned into words from Odoo's own field and value labels; nothing is guessed."""
        try:
            tree = ast.parse((invisible or "False").strip(), mode="eval").body
        except SyntaxError:
            return [([key for key, _label in stages], _("other conditions allow it"))]
        if not by_state:
            return [([], self._words(model, tree, True, by_state))]
        by_words = {}
        for key, _label in stages:
            rest = simplify(tree, key)  # the rule once the stage is known: what else it depends on there
            if not (isinstance(rest, ast.Constant) and rest.value):
                by_words.setdefault(self._words(model, rest, True, by_state), []).append(key)
        return [(keys, words) for words, keys in by_words.items()]

    @api.model
    def _words(self, model, node, negate, by_state):
        """`node` (or its opposite when negate) in plain words, leaving out what the stage list already says."""
        if by_state and "state" in {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} and not any(
                isinstance(n, ast.Name) and n.id != "state" for n in ast.walk(node)):
            return ""
        if isinstance(node, ast.Constant):
            return ""
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return self._words(model, node.operand, not negate, by_state)
        if isinstance(node, ast.BoolOp):
            joiner = _(" and ") if isinstance(node.op, ast.Or) == negate else _(" or ")
            other = _(" or ") if joiner == _(" and ") else _(" and ")
            parts = [self._words(model, value, negate, by_state) for value in node.values]
            parts = [f"({part})" if other in part else part for part in parts]  # "A and (B or C)"
            return joiner.join(dict.fromkeys(part for part in parts if part))
        name = (node.id if isinstance(node, ast.Name)
                else getattr(node.left, "id", None) if isinstance(node, ast.Compare) else None)
        if name == "id":
            return ""  # "the record is saved": not a business condition
        field = self.env[model]._fields.get(node.id) if isinstance(node, ast.Name) else None
        if field:
            if field.type == "boolean":
                return (_("not %s", field.string) if negate else field.string)
            return (_("%s is empty", field.string) if negate else _("%s is set", field.string))
        if (isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in OPPOSITE
                and isinstance(node.left, ast.Name) and node.left.id in self.env[model]._fields
                and literal(node.comparators[0]) is not None):
            field = self.env[model]._fields[node.left.id]
            names = dict(field._description_selection(self.env)) if field.type == "selection" else {}
            values = literal(node.comparators[0])
            values = values if isinstance(values, (list, tuple)) else [values]
            text = ", ".join(f'"{names.get(value, value)}"' if isinstance(value, str) else str(value)
                             for value in values)
            operator = OPPOSITE[type(node.ops[0])] if negate else type(node.ops[0])
            if operator in (ast.In, ast.NotIn) and len(values) == 1:
                operator = ast.Eq if operator is ast.In else ast.NotEq
            return f"{field.string} {OPERATOR_WORDS[operator]} {text}"
        return _("other conditions allow it")

    @api.model
    def _when(self, conditions, stages, by_state):
        """Text of the merged (stage keys, words) pairs of one action."""
        labels = dict(stages)
        conditions = merge_opposites(conditions)
        texts = []
        for words in dict.fromkeys(words for _keys, words in conditions):
            keys = {key for stage_keys, other in conditions if other == words for key in stage_keys}
            stage_text = (_("Any stage") if not by_state or len(keys) == len(stages)
                          else ", ".join(labels[key] for key, _label in stages if key in keys))
            texts.append(_("%(stages)s, when %(words)s", stages=stage_text, words=words) if words else stage_text)
        return _("; or ").join(texts)

    @api.model
    def _editors(self, model):
        """Who runs a button that names no group: the groups that may create this document (other apps often
        only update it), else those that may change it."""
        Access = self.env["ir.model.access"].sudo()
        access = (Access.search([("model_id.model", "=", model), ("perm_create", "=", True)])
                  or Access.search([("model_id.model", "=", model), ("perm_write", "=", True)]))
        if any(not rule.group_id for rule in access):
            return _("Everyone with access")
        names = sorted(set(access.group_id.mapped("full_name")))
        return ", ".join(names[:3]) + (_(" and others") if len(names) > 3 else "") or _("Administrators")

    @api.model
    def _actions(self, model, stages, by_state):
        """[(action, stages where it appears, who)] from the header buttons of the document's form view."""
        View = self.env["ir.ui.view"].sudo()
        view_id = View.default_view(model, "form")
        if not view_id:
            return []
        out, editors = {}, None
        for button in View.browse(view_id)._get_combined_arch().iterfind(".//header/button"):
            label = (button.get("string") or "").strip()
            if not label or label in SKIP_ACTIONS or button.get("invisible") in ("1", "True"):
                continue
            groups = [self.env.ref(xmlid.strip(), raise_if_not_found=False)
                      for xmlid in (button.get("groups") or "").split(",") if xmlid.strip() and xmlid.strip()[0] != "!"]
            groups = [group for group in groups if group and group._name == "res.groups"]
            if groups:
                who = ", ".join(group.full_name for group in groups)
            else:
                editors = editors or self._editors(model)
                who = editors
            available = self._available(model, button.get("invisible"), stages, by_state)
            if not available:
                continue  # hidden in every stage
            # The same action is often one button per stage ("Confirm" on a quotation, sent or not): merged.
            conditions, who = out.get(label, ([], who))
            out[label] = (conditions + available, who)
        return [(label, self._when(conditions, stages, by_state), who)
                for label, (conditions, who) in out.items()][:MAX_ACTIONS]

    @api.model
    def _custom_rules(self, model):
        """Error messages the custom code raises on this document, and its automation rules."""
        rules = []
        Index = self.env["ai_agent.flow.index"].sudo()
        for record in Index.search([("model_name", "=", model), ("messages", "!=", False)], limit=50):
            try:
                rules += [_("Blocks with: %s", message.replace("%%", "%")) for message in json.loads(record.messages)]
            except ValueError:
                continue
        for record in Index.search([("model_name", "=", model), ("kind", "=", "database")], limit=20):
            rules.append(record.source_path)
        return list(dict.fromkeys(rules))[:MAX_RULES]

    # ------------------------------------------------------------------
    # Building
    # ------------------------------------------------------------------
    @api.model
    def _flow_definitions(self, custom_module_names):
        flows = [(name, [(model, label) for model, label in steps if model in self.env])
                 for name, own_model, steps in STANDARD_FLOWS if own_model in self.env]
        known = {model for _name, _own, steps in STANDARD_FLOWS for model, _label in steps}
        # Document types this instance's own modules add, when they have stages.
        for record in self.env["ir.model"].sudo().search([("transient", "=", False)]):
            if record.model in known or record.model not in self.env:
                continue
            Model = self.env[record.model]
            if Model._abstract or getattr(Model, "_original_module", "") not in custom_module_names:
                continue
            if self._stages(record.model)[0]:
                flows.append((record.name, [(record.model, record.name)]))
        return [(name, steps) for name, steps in flows if steps]

    @api.model
    def _diagram(self, steps):
        """HTML of one flow: the document chain, then each document's stages, actions and custom rules."""
        arrow = Markup('<i class="fa fa-long-arrow-right text-muted mx-2"></i>')
        chip = Markup('<span class="badge rounded-pill text-bg-light border fw-normal fs-6 my-1">%s</span>')
        html = Markup('<div class="mb-4">%s</div>') % arrow.join(
            Markup('<span class="badge text-bg-primary fs-6 my-1">%s</span>') % label for label, *_rest in steps)
        for label, stages, actions, rules in steps:
            html += Markup('<h4 class="mt-4">%s</h4>') % label
            if stages:
                html += Markup('<div class="mb-2">%s</div>') % arrow.join(chip % name for _key, name in stages)
            if actions:
                rows = Markup().join(Markup("<tr><td>%s</td><td>%s</td><td>%s</td></tr>") % action
                                     for action in actions)
                html += Markup('<table class="table table-sm table-bordered w-auto mt-2"><thead><tr><th>%s</th>'
                               '<th>%s</th><th>%s</th></tr></thead><tbody>%s</tbody></table>') % (
                    _("Action"), _("Available in stage"), _("Who can do it"), rows)
            if rules:
                html += Markup('<div class="text-muted mt-2">%s</div><ul>%s</ul>') % (
                    _("Custom rules of this database:"),
                    Markup().join(Markup("<li>%s</li>") % rule for rule in rules))
        return html

    @api.model
    def _rebuild(self, custom_module_names):
        """Recreate every flow; return their text copies for the business-flow index."""
        self.sudo().search([]).unlink()
        chunks = []
        for sequence, (name, documents) in enumerate(self._flow_definitions(custom_module_names)):
            steps, read = [], []
            for model, label in documents:
                # One document whose form another module broke must not stop the update (nor the daily job).
                try:
                    with self.env.cr.savepoint():
                        stages, by_state = self._stages(model)
                        steps.append((label, stages, self._actions(model, stages, by_state), self._custom_rules(model)))
                        read.append(model)
                except Exception:
                    _logger.warning("Business Flow: %s (%s) skipped, it could not be read.", label, model, exc_info=True)
            if not steps:
                continue
            self.sudo().create({
                "name": name, "sequence": sequence, "documents": " → ".join(step[0] for step in steps),
                "stage_count": sum(len(step[1]) for step in steps), "action_count": sum(len(step[2]) for step in steps),
                "rule_count": sum(len(step[3]) for step in steps), "diagram": self._diagram(steps),
            })
            # One text per document, each headed by the whole journey: a whole flow (Sales has five documents) is
            # longer than one excerpt, and its last documents (delivery, invoice, payment) would be cut off.
            journey = _("Business flow: %(name)s. Documents in order: %(chain)s.", name=name,
                        chain=" -> ".join(step[0] for step in steps))
            for model, (label, stages, actions, rules) in zip(read, steps):
                text = [journey, _("%(doc)s stages: %(stages)s.", doc=label,
                                   stages=" -> ".join(stage for _key, stage in stages) or "-")]
                text += [_("%(doc)s action %(action)s%(when)s, done by %(who)s.", doc=label, action=action,
                           when=_(" (stage: %s)", when) if when else "", who=who) for action, when, who in actions]
                text += [f"{label}: {rule}" for rule in rules]
                chunks.append({"module_name": "business flow", "source_path": _("Business Flow: %s", name),
                               "line_start": 1, "kind": "flow", "model_name": model, "symbol": f"{name}: {label}",
                               "content": "\n".join(text)[:3000]})
        return chunks
