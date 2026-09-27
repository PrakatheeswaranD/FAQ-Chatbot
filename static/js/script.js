const chatForm = document.getElementById("chat-form");
const questionInput = document.getElementById("question");
const chatBox = document.getElementById("chat-box");
const sendButton = document.getElementById("send-button");
const clearButton = document.getElementById("clear-button");
const suggestionButtons = document.querySelectorAll(".suggestion");
const welcomeMessage = chatBox.firstElementChild;

const WORD_DELAY_MS = 30;
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const HISTORY_KEY = "kit-faq-chat-history";
const MAX_SAVED_MESSAGES = 50;

const LINK_PATTERN =
    /([\w.+-]+@[\w-]+(?:\.[\w-]+)+)|((?:https?:\/\/)?(?:[\w-]+\.)+(?:com|in|org)\b(?:\/[\w\-\/]*)?)|(\+?\d[\d \-]{8,}\d)/gi;

let chatHistory = loadHistory();
restoreHistory();

suggestionButtons.forEach(function (button) {
    button.addEventListener("click", function () {
        questionInput.value = button.dataset.question || button.textContent.trim();
        questionInput.focus();
    });
});


chatForm.addEventListener("submit", async function (event) {
    event.preventDefault();
    const question = questionInput.value.trim();
    if (question === "") {
        return;
    }

    addMessage(question, "user");
    saveMessage({ type: "user", text: question });
    questionInput.value = "";
    setBusy(true);
    const typing = showTypingIndicator();

    try {
        const response = await fetch("/chat", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                question: question
            })
        });
        const data = await response.json().catch(() => ({}));
        typing.remove();

        if (response.ok) {
            await addBotAnswer(data);
        } else {
            addMessage(data.error || "Unable to get an answer.", "error");
        }
    } catch (error) {
        console.error("Request error:", error);
        typing.remove();
        addMessage("Unable to connect to the server.", "error");
    } finally {
        setBusy(false);
        questionInput.focus();
    }
});


function setBusy(busy) {
    sendButton.disabled = busy;
    sendButton.textContent = busy ? "Sending..." : "Send";
}


function scrollToBottom() {
    chatBox.scrollTop = chatBox.scrollHeight;
}


function addMessage(text, sender) {
    const message = document.createElement("div");
    message.classList.add("message");

    if (sender === "user") {
        message.classList.add("user-message");
    } else if (sender === "error") {
        message.classList.add("error-message");
    } else {
        message.classList.add("bot-message");
    }

    message.textContent = text;
    chatBox.appendChild(message);
    scrollToBottom();
    return message;
}


function showTypingIndicator() {
    const indicator = document.createElement("div");
    indicator.className = "message bot-message typing-indicator";
    indicator.setAttribute("aria-label", "The bot is typing");
    for (let i = 0; i < 3; i++) {
        indicator.appendChild(document.createElement("span"));
    }
    chatBox.appendChild(indicator);
    scrollToBottom();
    return indicator;
}


async function addBotAnswer(data) {
    const entry = {
        type: "bot",
        text: data.answer || "",
        sources: data.sources || [],
        id: data.id || null,
        rating: null
    };
    saveMessage(entry);
    await renderBotAnswer(entry, true);
}


async function renderBotAnswer(entry, animate) {
    const wrapper = document.createElement("div");
    wrapper.className = "bot-answer";

    const message = document.createElement("div");
    message.className = "message bot-message";
    wrapper.appendChild(message);
    chatBox.appendChild(wrapper);

    if (animate) {
        message.setAttribute("aria-busy", "true");
        await typeOut(message, entry.text);
        message.setAttribute("aria-busy", "false");
    }
    linkify(message, entry.text);

    if (entry.sources.length > 0) {
        wrapper.appendChild(buildSources(entry.sources));
    }
    if (entry.id) {
        wrapper.appendChild(buildFeedback(entry));
    }
    scrollToBottom();
}


function linkify(element, text) {
    element.textContent = "";
    let last = 0;

    for (const match of text.matchAll(LINK_PATTERN)) {
        const [value, email, website, phone] = match;
        let href = null;

        if (email) {
            href = "mailto:" + email;
        } else if (website) {
            href = /^https?:/i.test(website) ? website : "https://" + website;
        } else if (phone) {
            const digits = phone.replace(/\D/g, "");
            if (digits.length >= 10 && digits.length <= 13) {
                href = "tel:" + (phone.trim().startsWith("+") ? "+" : "") + digits;
            }
        }
        if (!href) {
            continue;
        }

        element.appendChild(document.createTextNode(text.slice(last, match.index)));
        const link = document.createElement("a");
        link.href = href;
        link.textContent = value;
        if (website) {
            link.target = "_blank";
            link.rel = "noopener noreferrer";
        }
        element.appendChild(link);
        last = match.index + value.length;
    }
    element.appendChild(document.createTextNode(text.slice(last)));
}


function loadHistory() {
    try {
        const saved = JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
        return Array.isArray(saved) ? saved : [];
    } catch (error) {
        return [];
    }
}


function storeHistory() {
    try {
        localStorage.setItem(HISTORY_KEY, JSON.stringify(chatHistory));
    } catch (error) {
    }
}


function saveMessage(entry) {
    chatHistory.push(entry);
    chatHistory = chatHistory.slice(-MAX_SAVED_MESSAGES);
    storeHistory();
}


function restoreHistory() {
    chatHistory.forEach(function (entry) {
        if (entry.type === "user") {
            addMessage(entry.text, "user");
        } else if (entry.type === "bot") {
            renderBotAnswer({
                text: String(entry.text || ""),
                sources: Array.isArray(entry.sources) ? entry.sources : [],
                id: entry.id || null,
                rating: entry.rating || null
            }, false);
        }
    });
}


clearButton.addEventListener("click", function () {
    chatHistory = [];
    storeHistory();
    while (welcomeMessage.nextSibling) {
        welcomeMessage.nextSibling.remove();
    }
    questionInput.focus();
});


function typeOut(element, text) {
    if (reduceMotion) {
        element.textContent = text;
        return Promise.resolve();
    }

    const pieces = text.split(/(\s+)/);
    let index = 0;

    return new Promise(function (resolve) {
        function step() {
            if (index >= pieces.length) {
                resolve();
                return;
            }
            element.textContent += pieces[index] + (pieces[index + 1] || "");
            index += 2;
            scrollToBottom();
            setTimeout(step, WORD_DELAY_MS);
        }
        step();
    });
}


function buildSources(sources) {
    const box = document.createElement("div");
    box.className = "answer-sources";

    const label = document.createElement("span");
    label.className = "sources-label";
    label.textContent = sources.length === 1 ? "Source:" : "Sources:";
    box.appendChild(label);

    const list = document.createElement("ul");
    sources.forEach(function (source) {
        const item = document.createElement("li");
        item.textContent = source;
        list.appendChild(item);
    });
    box.appendChild(list);
    return box;
}


function buildFeedback(entry) {
    const box = document.createElement("div");
    box.className = "answer-feedback";

    const prompt = document.createElement("span");
    prompt.textContent = "Was this helpful?";
    box.appendChild(prompt);

    const buttons = [
        { rating: "up", symbol: "\u{1F44D}", label: "Helpful" },
        { rating: "down", symbol: "\u{1F44E}", label: "Not helpful" }
    ].map(function (option) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "feedback-button";
        button.textContent = option.symbol;
        button.setAttribute("aria-label", option.label);
        button.setAttribute("aria-pressed", "false");
        button.addEventListener("click", function () {
            sendFeedback(entry, option.rating, button, buttons, prompt);
        });
        box.appendChild(button);
        return button;
    });

    if (entry.rating) {
        markSelected(buttons, buttons[entry.rating === "up" ? 0 : 1]);
    }

    return box;
}


function markSelected(buttons, clicked) {
    buttons.forEach(function (button) {
        button.setAttribute("aria-pressed", String(button === clicked));
        button.classList.toggle("selected", button === clicked);
    });
}


async function sendFeedback(entry, rating, clicked, buttons, prompt) {
    buttons.forEach(function (button) {
        button.disabled = true;
    });

    try {
        const response = await fetch("/feedback", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({ id: entry.id, rating: rating })
        });
        if (!response.ok) {
            throw new Error("Feedback not saved");
        }
        markSelected(buttons, clicked);
        entry.rating = rating;
        storeHistory();
        prompt.textContent = rating === "up"
            ? "Thanks for your feedback!"
            : "Thanks! We'll use this to improve the answers.";
    } catch (error) {
        console.error("Feedback error:", error);
        prompt.textContent = "Couldn't save feedback. Try again.";
    } finally {
        buttons.forEach(function (button) {
            button.disabled = false;
        });
    }
}
