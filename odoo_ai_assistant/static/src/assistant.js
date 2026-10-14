import {
    Component, markup, onMounted, onPatched, onWillStart, onWillUnmount, reactive, useEffect, useExternalListener,
    useRef, useState,
} from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { downloadFile } from "@web/core/network/download";
import { Dropdown } from "@web/core/dropdown/dropdown";
import { DropdownItem } from "@web/core/dropdown/dropdown_item";
import { user } from "@web/core/user";
import { deserializeDateTime } from "@web/core/l10n/dates";
import { DateSection } from "@mail/core/common/date_section";
import { generatePdfThumbnail } from "@mail/utils/common/pdf_thumbnail";
import { LocalVisualization, visualizationContent, visualizationPayload } from "./visualization";

const { DateTime } = luxon;

const MODEL = "odoo.ai.assistant.conversation";

registry.category("services").add("odoo_ai", {
    dependencies: ["orm", "notification", "action", "bus_service", "menu"],
    async start(env, { orm, notification, action, bus_service, menu }) {
        const state = reactive({
            enabled: false, conversations: [], messages: [], current: null,
            open: false, minimized: false, busy: false, discussActive: false,
            provider: "", model: "", can_stage_files: false, error: "", progress: "",
            previousChat: null, showResume: false,
            assistant_name: "", suggestions: [], max_message_chars: 0, max_upload_bytes: 0,
            channel_ids: [],
        });
        const service = {
            state,
            async refresh() {
                const data = await orm.call(MODEL, "bootstrap", []);
                Object.assign(state, data);
            },
            /** Run one server action at a time: report failures and return `fallback` instead. */
            async run(task, { fallback, progress = "", needsChat = false } = {}) {
                if (state.busy || (needsChat && !state.current)) {
                    return fallback;
                }
                state.busy = true;
                state.error = "";
                state.progress = progress;
                try {
                    return await task();
                } catch (error) {
                    service.report(error);
                    return fallback;
                } finally {
                    state.busy = false;
                    state.progress = "";
                }
            },
            select(conversation) {
                return service.run(async () => {
                    const messages = await orm.call(MODEL, "get_messages", [[conversation.id]]);
                    state.current = conversation;
                    state.messages = messages;
                    state.showResume = false;
                    await service.refresh();
                });
            },
            newChat() {
                return service.run(async () => {
                    const previous = state.current || state.conversations[0] || null;
                    const controller = action.currentController;
                    const recordId = controller?.currentState?.resId;
                    const recordModel = controller?.props?.resModel;
                    const fromRecord = controller?.view?.type === "form" &&
                        typeof recordModel === "string" && Number.isInteger(recordId) && recordId > 0;
                    const conversation = await orm.call(
                        MODEL, "start_conversation", [],
                        fromRecord ? { record_model: recordModel, record_id: recordId } : {},
                    );
                    state.current = conversation;
                    state.messages = [];
                    state.previousChat = previous;
                    state.showResume = Boolean(previous);
                    await service.refresh();
                    return conversation;
                });
            },
            /** Where the user is: the screen's title, never a record's name (Odoo checks it against their menus). */
            currentScreen() {
                const controller = action.currentController;
                return {
                    app: menu.getCurrentApp()?.name,
                    screen: controller?.action?.name,
                    view_type: controller?.view?.type,
                    model: controller?.props?.resModel,
                };
            },
            report(error) {
                state.error = error.data?.message || error.message || _t("AI request failed.");
                notification.add(state.error, { type: "danger" });
            },
            async resumePrevious() {
                const previous = state.previousChat;
                if (!state.busy && previous) {
                    await service.select(previous);
                }
            },
            /** Send the text with the staged files (cards from stageUpload) it refers to. */
            async send(content, files = []) {
                if (state.busy || (!content.trim() && !files.length)) {
                    return false;
                }
                if (!state.current && !(await service.newChat())) {
                    return false;
                }
                return service.run(async () => {
                    state.messages = await orm.call(MODEL, "send_message", [
                        [state.current.id], content, files.map((file) => file.reference), service.currentScreen(),
                    ]);
                    await service.refresh();
                    state.current = state.conversations.find((c) => c.id === state.current.id);
                    return true;
                }, { fallback: false, progress: _t("Checking Odoo topic") });
            },
            /** Save a chart or table of the current conversation as a PDF or Excel file. */
            exportChart(messageId, format, image) {
                return service.run(async () => {
                    const file = await orm.call(MODEL, "export_chart", [[state.current.id], messageId, format, image]);
                    // As bytes: Odoo's helper only recognises data URLs whose type has no dots (not .xlsx).
                    const bytes = Uint8Array.from(atob(file.data), (char) => char.charCodeAt(0));
                    downloadFile(new Blob([bytes], { type: file.mimetype }), file.name, file.mimetype);
                }, { needsChat: true, progress: _t("Preparing the export") });
            },
            loadRecordPage(messageId, previous) {
                return service.run(async () => {
                    state.messages = await orm.call(MODEL, "load_record_page", [[state.current.id], messageId, previous]);
                    return true;
                }, { fallback: false, needsChat: true, progress: _t("Loading the record page") });
            },
            toolAction(method, requestId) {
                return service.run(async () => {
                    state.messages = await orm.call(MODEL, method, [[state.current.id], requestId]);
                    await service.refresh();
                    return true;
                }, { fallback: false, needsChat: true });
            },
            async stageUpload(file) {
                if (state.busy) {
                    return null;
                }
                if (!state.current && !(await service.newChat())) {
                    return null;
                }
                return service.run(async () => {
                    if (file.size > state.max_upload_bytes) {
                        const limit = (state.max_upload_bytes / (1024 * 1024)).toFixed(1);
                        throw new Error(_t("Choose a file no larger than %s MiB.", limit));
                    }
                    const data = await new Promise((resolve, reject) => {
                        const reader = new FileReader();
                        reader.onerror = () => reject(new Error(_t("Unable to read the selected file.")));
                        reader.onload = () => resolve(String(reader.result).split(",")[1]);
                        reader.readAsDataURL(file);
                    });
                    // As Discuss does for PDF attachments: the browser draws the first page as the card preview.
                    const { thumbnail } = file.type === "application/pdf"
                        ? await generatePdfThumbnail(`data:${file.type};base64,${data}`).catch(() => ({}))
                        : {};
                    return orm.call(MODEL, "stage_upload", [
                        [state.current.id], file.name, data, file.type || null, thumbnail || null,
                    ]);
                }, { fallback: null });
            },
            remove(conversation) {
                return service.run(async () => {
                    await orm.unlink(MODEL, [conversation.id]);
                    if (state.previousChat?.id === conversation.id) {
                        state.previousChat = null;
                        state.showResume = false;
                    }
                    if (state.current?.id === conversation.id) {
                        state.current = null;
                        state.messages = [];
                    }
                    await service.refresh();
                });
            },
            async open() {
                if (state.busy) {
                    return;
                }
                try {
                    await service.refresh();
                    if (action.currentController?.action.tag === "mail.action_discuss") {
                        if (await service.newChat()) {
                            await service.discuss();
                        }
                        return;
                    }
                    state.open = true;
                    state.minimized = false;
                    await service.newChat();
                } catch (error) {
                    service.report(error);
                }
            },
            /** Show the AI workspace inside Discuss with its current (or latest) conversation. */
            async showWorkspace() {
                state.open = false;
                state.discussActive = true;
                await service.refresh();
                if (state.current) {
                    if (!state.showResume) {
                        await service.select(state.current);
                    }
                } else if (state.conversations.length) {
                    await service.select(state.conversations[0]);
                }
            },
            async discuss() {
                if (state.busy) {
                    return;
                }
                try {
                    await service.refresh();
                    state.open = false;
                    state.minimized = false;
                    state.discussActive = true;
                    await action.doAction("mail.action_discuss", {
                        additionalContext: { active_id: "odoo_ai.workspace", odoo_ai_workspace: true },
                    });
                } catch (error) {
                    service.report(error);
                }
            },
        };
        bus_service.subscribe("odoo_ai_progress", ({ conversation_id, label }) => {
            if (state.busy && state.current?.id === conversation_id && typeof label === "string") {
                state.progress = label;
            }
        });
        await service.refresh();
        return service;
    },
});

export class AssistantPanel extends Component {
    static template = "odoo_ai_assistant.Panel";
    static components = { LocalVisualization, DateSection, Dropdown, DropdownItem };
    static props = { full: { type: Boolean, optional: true } };

    setup() {
        this.ai = useService("odoo_ai");
        this.dialog = useService("dialog");
        this.notification = useService("notification");
        this.state = useState(this.ai.state);
        this.input = useState({ text: "", search: "", filter: "all", files: [] });
        this.scrollState = useState({ awayFromBottom: false });
        this.userName = user.name;
        this.userInitial = user.name.trim().charAt(0).toLocaleUpperCase();
        this.messagesRef = useRef("messages");
        this.composerRef = useRef("composer");
        this.fileInputRef = useRef("fileInput");
        this.lastMessage = null;
        this.lastConversation = null;
        this.formattedMessages = new WeakMap();
        this.voice = useState({ recording: false, interim: "", error: "" });
        this.recognition = null;
        this.voiceMounted = true;
        useExternalListener(window, "keydown", (event) => {
            if (event.altKey && !event.ctrlKey && !event.metaKey && event.key.toLowerCase() === "v" &&
                (this.props.full
                    ? this.state.discussActive && !this.state.open
                    : this.state.open && !this.state.minimized)) {
                event.preventDefault();
                this.toggleVoice();
            }
        });
        useEffect(() => {
            this.stopVoice();
        }, () => [this.state.current?.id, this.state.open, this.state.minimized, this.state.busy]);
        onWillUnmount(() => {
            this.voiceMounted = false;
            this.stopVoice();
        });
        onMounted(() => this.scrollMessages());
        onPatched(() => this.scrollMessages());
    }

    messagePayload(message) {
        return visualizationPayload(message.payload);
    }

    shareResult(requestId) {
        this.dialog.add(ConfirmationDialog, {
            title: _t("Share Odoo data with your AI provider?"),
            body: _t("This sends the displayed result and any inline attachment content to %s. Only approve information you are permitted to disclose. This cannot undo disclosure after the provider receives it.", this.state.provider),
            confirmLabel: _t("Share and continue"),
            confirm: () => this.ai.toolAction("share_tool_result", requestId),
        });
    }

    async attachFile(event) {
        const file = event.target.files?.[0];
        event.target.value = "";
        if (!file) {
            return;
        }
        const card = await this.ai.stageUpload(file);
        if (card) {
            this.input.files.push(card);
            this.composerRef.el?.focus();
        }
    }

    removeFile(index) {
        this.input.files.splice(index, 1);
    }

    messageContent(message) {
        return visualizationContent(message);
    }

    messageHTML(message) {
        // Only server-sanitized assistant replies are eligible for HTML rendering.
        if (message.role === "assistant" && !message.local_only &&
            !message.payload && typeof message.content_html === "string") {
            const cached = this.formattedMessages.get(message);
            if (cached?.source === message.content_html) {
                return cached.html;
            }
            const html = markup(message.content_html);
            this.formattedMessages.set(message, { source: message.content_html, html });
            return html;
        }
        return null;
    }

    scrollMessages() {
        const last = this.state.messages.at(-1)?.id;
        const conversation = this.state.current?.id;
        if (this.messagesRef.el && (last !== this.lastMessage || conversation !== this.lastConversation)) {
            if (conversation !== this.lastConversation || !this.scrollState.awayFromBottom) {
                this.jumpToLatest();
            }
            this.lastMessage = last;
            this.lastConversation = conversation;
        }
    }

    onMessagesScroll() {
        const element = this.messagesRef.el;
        if (element) {
            this.scrollState.awayFromBottom = element.scrollHeight - element.scrollTop - element.clientHeight > 48;
        }
    }

    jumpToLatest() {
        const element = this.messagesRef.el;
        if (element) {
            element.scrollTop = element.scrollHeight;
            this.onMessagesScroll();
        }
    }

    messageDateTime(message) {
        return message.create_date ? deserializeDateTime(message.create_date, { tz: user.context.tz }) : null;
    }

    // Same labels as stock Discuss (mail Message.dateDay / dateSimpleWithDay).
    messageDay(message) {
        const datetime = this.messageDateTime(message);
        if (!datetime) {
            return "";
        }
        const day = datetime.toLocaleString(DateTime.DATE_MED);
        return day === DateTime.now().toLocaleString(DateTime.DATE_MED) ? _t("Today") : day;
    }

    startsNewDay(index) {
        const day = this.messageDay(this.state.messages[index]);
        return Boolean(day) && (index === 0 || this.messageDay(this.state.messages[index - 1]) !== day);
    }

    messageTime(message) {
        const datetime = this.messageDateTime(message);
        if (!datetime) {
            return "";
        }
        const locale = { locale: user.lang };
        const now = DateTime.now();
        if (datetime.hasSame(now, "day")) {
            return datetime.toLocaleString(DateTime.TIME_SIMPLE, locale);
        }
        if (datetime.hasSame(now.minus({ day: 1 }), "day")) {
            return _t("Yesterday at %(time)s", { time: datetime.toLocaleString(DateTime.TIME_SIMPLE, locale) });
        }
        if (datetime.year === now.year) {
            return datetime.toLocaleString({ ...DateTime.DATETIME_MED, year: undefined }, locale);
        }
        return datetime.toLocaleString(DateTime.DATETIME_MED, locale);
    }

    messageDateTitle(message) {
        const datetime = this.messageDateTime(message);
        return datetime ? datetime.toLocaleString({
            year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
        }) : "";
    }

    async send() {
        this.stopVoice();
        const text = this.input.text;
        if (await this.ai.send(text, this.input.files)) {
            this.input.text = "";
            this.input.files = [];
            this.composerRef.el?.focus();
        }
    }

    stopVoice() {
        if (this.recognition) {
            const recognition = this.recognition;
            this.recognition = null;
            recognition.onresult = recognition.onerror = recognition.onend = null;
            recognition.abort();
        }
        this.voice.recording = false;
        this.voice.interim = "";
    }

    toggleVoice() {
        if (this.state.busy) {
            return;
        }
        if (this.voice.recording) {
            this.recognition?.stop();
            return;
        }
        const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
        if (!Recognition || !window.isSecureContext) {
            this.voice.error = _t("Speech recognition is unavailable. Use a supported browser on HTTPS or localhost.");
            this.notification.add(this.voice.error, { type: "warning" });
            return;
        }
        this.voice.error = "";
        const conversationId = this.state.current?.id;
        this.dialog.add(ConfirmationDialog, {
            title: _t("Voice input"),
            body: _t("Your browser may send microphone audio to its speech-recognition service. Odoo does not save audio. Recognized text stays editable and is not sent to your AI provider until you press Send."),
            confirmLabel: _t("Start microphone"),
            confirm: () => {
                if (this.state.current?.id === conversationId) {
                    this.startVoice(Recognition);
                }
            },
        });
    }

    startVoice(Recognition) {
        if (!this.voiceMounted || this.state.busy || this.recognition ||
            (this.props.full ? !this.state.discussActive : !this.state.open || this.state.minimized)) {
            return;
        }
        const recognition = new Recognition();
        recognition.lang = (user.lang || navigator.language).replace("_", "-");
        recognition.continuous = true;
        recognition.interimResults = true;
        recognition.onresult = (event) => {
            let interim = "";
            for (let index = event.resultIndex; index < event.results.length; index++) {
                const result = event.results[index];
                if (result.isFinal) {
                    const text = [this.input.text.trim(), result[0].transcript.trim()].filter(Boolean).join(" ");
                    if (text.length > this.state.max_message_chars) {
                        this.voice.error = _t("Voice input exceeds the %s-character message limit.",
                            this.state.max_message_chars.toLocaleString());
                        this.notification.add(this.voice.error, { type: "warning" });
                        this.stopVoice();
                        return;
                    }
                    this.input.text = text;
                } else {
                    interim += result[0].transcript;
                }
            }
            this.voice.interim = interim;
        };
        recognition.onerror = (event) => {
            this.voice.error = _t("Speech recognition failed (%s). Check microphone permission and retry.", event.error);
            this.notification.add(this.voice.error, { type: "warning" });
            this.stopVoice();
        };
        recognition.onend = () => this.stopVoice();
        this.recognition = recognition;
        try {
            recognition.start();
            this.voice.recording = true;
        } catch (error) {
            this.stopVoice();
            this.voice.error = error.message || _t("Unable to start microphone.");
            this.notification.add(this.voice.error, { type: "danger" });
        }
    }

    suggest(text) {
        this.input.text = text;
        this.composerRef.el?.focus();
    }

    keydown(event) {
        if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
            event.preventDefault();
            this.send();
        }
    }

    expand() {
        return this.ai.discuss();
    }

    get filteredConversations() {
        const query = this.input.search.trim().toLocaleLowerCase();
        return this.state.conversations.filter((conversation) =>
            (this.input.filter !== "unread" || conversation.unread) &&
            (!query || conversation.name.toLocaleLowerCase().includes(query) ||
                (conversation.preview || "").toLocaleLowerCase().includes(query))
        );
    }

    conversationDate(conversation) {
        if (!conversation.date) {
            return "";
        }
        const date = new Date(conversation.date.replace(" ", "T") + "Z");
        const today = new Date();
        return date.toDateString() === today.toDateString()
            ? date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
            : date.toLocaleDateString([], { month: "short", day: "numeric" });
    }

    deleteChat(conversation) {
        this.dialog.add(ConfirmationDialog, {
            body: _t("Delete this AI conversation and its Discuss history?"),
            confirm: () => this.ai.remove(conversation),
        });
    }
}

class AssistantOverlay extends Component {
    static template = "odoo_ai_assistant.Overlay";
    static components = { AssistantPanel };
    static props = {};

    setup() {
        this.state = useState(useService("odoo_ai").state);
        this.size = useState({ width: 0, height: 0 });
        this.resizeStart = null;
    }

    keydown(event) {
        if (event.key === "Escape") {
            this.state.open = false;
        }
    }

    startResize(event) {
        if (this.state.minimized) {
            return;
        }
        const box = event.currentTarget.parentElement.getBoundingClientRect();
        this.resizeStart = { x: event.clientX, y: event.clientY, width: box.width, height: box.height };
        event.currentTarget.setPointerCapture(event.pointerId);
        event.preventDefault();
    }

    resize(event) {
        if (!this.resizeStart) {
            return;
        }
        const viewport = event.currentTarget.ownerDocument.documentElement;
        this.size.width = Math.min(
            Math.max(340, this.resizeStart.width + this.resizeStart.x - event.clientX),
            viewport.clientWidth - 32,
        );
        this.size.height = Math.min(
            Math.max(300, this.resizeStart.height + this.resizeStart.y - event.clientY),
            viewport.clientHeight - 76,
        );
    }

    stopResize() {
        this.resizeStart = null;
    }
}

class AssistantSystray extends Component {
    static template = "odoo_ai_assistant.Systray";
    static props = {};

    setup() {
        this.ai = useService("odoo_ai");
        this.state = useState(this.ai.state);
    }
}

class AssistantAction extends Component {
    static template = "odoo_ai_assistant.Action";
    static components = { AssistantPanel };
    static props = ["*"];

    setup() {
        this.ai = useService("odoo_ai");
        // Render the full chat inside the AI app so its Reporting and Configuration
        // menus stay in the navbar; Discuss remains reachable from the chat pop-up.
        onWillStart(async () => {
            this.ai.state.open = false;
            await this.ai.refresh();
            if (this.ai.state.current) {
                await this.ai.select(this.ai.state.current);
            } else if (this.ai.state.conversations.length) {
                await this.ai.select(this.ai.state.conversations[0]);
            }
        });
        onMounted(() => (this.ai.state.discussActive = true));
        onWillUnmount(() => (this.ai.state.discussActive = false));
    }
}

registry.category("systray").add("odoo_ai_assistant.wand", { Component: AssistantSystray }, { sequence: 35 });
registry.category("main_components").add("odoo_ai_assistant.overlay", { Component: AssistantOverlay });
registry.category("actions").add("odoo_ai_assistant.assistant", AssistantAction);
