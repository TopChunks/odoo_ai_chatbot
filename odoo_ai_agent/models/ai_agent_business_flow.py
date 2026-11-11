"""The Business Flow menu: each process of this database as its documents, their stages, the actions that move
them and who may run each action, plus the custom rules found in the business-flow index.

Built with the context update from the database itself (status fields, stage records, form view buttons, access
rights). No AI provider is called, so building it costs no tokens. A text copy of each flow goes into the index,
so chat questions such as "what happens after the quotation?" find it.
"""

import json
import re

from markupsafe import Markup

from odoo import _, api, fields, models

# (flow, its documents in order). A document is (model, what users call it); missing apps are skipped.
STANDARD_FLOWS = [
    ("Sales", [("crm.lead", "Opportunity"), ("sale.order", "Quotation / Sales Order"),
               ("stock.picking", "Delivery"), ("account.move", "Customer Invoice"),
               ("account.payment", "Customer Payment")]),
    ("Purchase", [("purchase.order", "Request for Quotation / Purchase Order"), ("stock.picking", "Receipt"),
                  ("account.move", "Vendor Bill"), ("account.payment", "Vendor Payment")]),
    ("Manufacturing", [("mrp.production", "Manufacturing Order")]),
    ("Inventory", [("stock.picking", "Transfer")]),
    ("Invoicing", [("account.move", "Invoice / Bill"), ("account.payment", "Payment")]),
    ("Projects", [("project.task", "Task")]),
    ("Helpdesk", [("helpdesk.ticket", "Ticket")]),
    ("Repairs", [("repair.order", "Repair Order")]),
    ("Time Off", [("hr.leave", "Time Off Request")]),
    ("Expenses", [("hr.expense", "Expense")]),
]
STATE_CLAUSE = re.compile(r"\(?\s*state\s*(not in|in|!=|==)\s*(\[[^\]]*\]|\([^)]*\)|'[^']*'|\"[^\"]*\")\s*\)?")
SKIP_ACTIONS = {"Print", "Preview"}  # on every document, not a step of the flow
MAX_STAGES = 12
MAX_ACTIONS = 15
MAX_RULES = 8


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
    def _shown_in(self, invisible, stages):
        """Labels of the stages where a button with this `invisible` condition can appear ('' if unclear)."""
        clauses = [part for part in re.split(r"\s+or\s+", invisible or "") if part.strip()]
        for clause in clauses:
            match = STATE_CLAUSE.fullmatch(clause.strip())
            if not match:
                continue
            listed = {a or b for a, b in re.findall(r"'([^']*)'|\"([^\"]*)\"", match.group(2))}
            hidden_when_listed = match.group(1) in ("in", "==")
            return ", ".join(label for key, label in stages if (key in listed) != hidden_when_listed)
        return "" if clauses else _("Any stage")

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
            when = self._shown_in(button.get("invisible"), stages) if by_state else ""
            if label in out:  # the same action shown per stage ("Confirm" on a quotation, sent or not)
                whens = (out[label][0], when)
                when = ("" if not all(whens) else _("Any stage") if _("Any stage") in whens
                        else ", ".join(name for _key, name in stages if name in ", ".join(whens).split(", ")))
                who = out[label][1]
            out[label] = (when, who)
        return [(label, when, who) for label, (when, who) in out.items()][:MAX_ACTIONS]

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
                 for name, steps in STANDARD_FLOWS]
        known = {model for _name, steps in STANDARD_FLOWS for model, _label in steps}
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
            steps, text = [], [_("Business flow: %(name)s. Documents in order: %(chain)s.", name=name,
                                 chain=" -> ".join(label for _model, label in documents))]
            for model, label in documents:
                stages, by_state = self._stages(model)
                actions = self._actions(model, stages, by_state)
                rules = self._custom_rules(model)
                steps.append((label, stages, actions, rules))
                text.append(_("%(doc)s stages: %(stages)s.", doc=label,
                              stages=" -> ".join(stage for _key, stage in stages) or "-"))
                text += [_("%(doc)s action %(action)s%(when)s, done by %(who)s.", doc=label, action=action,
                           when=_(" (stage: %s)", when) if when else "", who=who) for action, when, who in actions]
                text += [f"{label}: {rule}" for rule in rules]
            self.sudo().create({
                "name": name, "sequence": sequence, "documents": " → ".join(label for _model, label in documents),
                "stage_count": sum(len(step[1]) for step in steps), "action_count": sum(len(step[2]) for step in steps),
                "rule_count": sum(len(step[3]) for step in steps), "diagram": self._diagram(steps),
            })
            chunks.append({"module_name": "business flow", "source_path": _("Business Flow: %s", name),
                           "line_start": 1, "kind": "flow", "model_name": documents[0][0], "symbol": name,
                           "content": "\n".join(text)[:3000]})
        return chunks
