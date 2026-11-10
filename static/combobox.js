/* Searchable patient dropdown (see templates/_combobox.html). */
(function () {
    function norm(text) { return (text || "").toLowerCase(); }
    function make(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function init(root) {
        var input = root.querySelector(".combo-input"), list = root.querySelector(".combo-list");
        var hidden = root.querySelector(".combo-value"), form = input.form;
        var data = JSON.parse(document.getElementById(root.id + "-data").textContent);
        var pickOnly = root.dataset.mode === "id", submit = root.dataset.submit === "1";
        var shown = [], active = -1;

        function matches(query) {
            var terms = norm(query).split(/\s+/).filter(Boolean);
            var found = data.filter(function (p) {
                var haystack = norm(p.n + " " + p.u + " " + p.id + " " + p.id.replace("-", ""));
                return terms.every(function (t) { return haystack.indexOf(t) > -1; });
            });
            return found.slice(0, 50);
        }
        function setActive(index) {
            var items = list.querySelectorAll("[role=option]");
            active = index;
            items.forEach(function (item, i) {
                var on = i === index;
                item.classList.toggle("active", on);
                item.setAttribute("aria-selected", on ? "true" : "false");
                if (on) { item.scrollIntoView({ block: "nearest" }); input.setAttribute("aria-activedescendant", item.id); }
            });
        }
        function open() {
            shown = matches(input.value);
            list.textContent = "";
            if (!shown.length) {
                var typed = input.value.trim();
                list.appendChild(make("li", "combo-empty", pickOnly || !typed
                    ? "No matching patients"
                    : "No matching patient. Press Enter to use “" + typed + "” as a new patient name."));
            }
            shown.forEach(function (p, i) {
                var item = make("li", "combo-option");
                item.id = root.id + "-opt-" + i;
                item.setAttribute("role", "option");
                item.appendChild(make("span", "mini-avatar neutral", (p.n || "?").charAt(0).toUpperCase()));
                item.appendChild(make("span", "combo-name", p.n));
                item.appendChild(make("span", "acct-id", p.id));
                item.appendChild(make("span", "combo-user", p.u));
                item.addEventListener("mousedown", function (event) { event.preventDefault(); pick(i); });
                item.addEventListener("mousemove", function () { if (active !== i) setActive(i); });
                list.appendChild(item);
            });
            list.hidden = false;
            input.setAttribute("aria-expanded", "true");
            setActive(shown.length && input.value.trim() ? 0 : -1);  // first match is highlighted once something is typed
        }
        function close() {
            list.hidden = true;
            input.setAttribute("aria-expanded", "false");
            input.removeAttribute("aria-activedescendant");
        }
        function pick(index) {
            var p = shown[index];
            if (!p) return;
            input.value = p.n;
            if (hidden) hidden.value = p.u;
            input.setCustomValidity("");
            close();
            if (submit && form) form.submit();
        }

        input.addEventListener("focus", open);
        input.addEventListener("click", function () { if (list.hidden) open(); });
        input.addEventListener("input", function () {
            if (hidden) hidden.value = "";
            input.setCustomValidity("");
            open();
        });
        input.addEventListener("blur", close);
        input.addEventListener("keydown", function (event) {
            if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                event.preventDefault();
                if (list.hidden) { open(); return; }
                if (!shown.length) return;
                var step = event.key === "ArrowDown" ? 1 : -1;
                setActive((active + step + shown.length) % shown.length);
            } else if (event.key === "Enter") {
                if (!list.hidden && active > -1) { event.preventDefault(); pick(active); }
                else if (pickOnly) event.preventDefault();
            } else if (event.key === "Escape") {
                close();
            }
        });
        root.querySelector(".combo-caret").addEventListener("mousedown", function (event) {
            event.preventDefault();
            if (list.hidden) { input.focus(); open(); } else close();
        });
        if (pickOnly && form) {
            form.addEventListener("submit", function (event) {
                if (!hidden.value) {
                    event.preventDefault();
                    input.setCustomValidity("Choose a patient from the list.");
                    input.reportValidity();
                }
            });
        }
    }

    document.addEventListener("DOMContentLoaded", function () {
        document.querySelectorAll(".combo").forEach(init);
    });
})();
