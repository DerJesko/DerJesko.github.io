// Fill in name and homepage for every <a class="people xyz"> from people.js.
for (const [id, data] of Object.entries(people)) {
    for (const link of document.querySelectorAll("a." + id)) {
        link.href = data["website"];
        link.innerText = data["name"];
    }
}

// Build the "People" section list from people.js.
const peopleList = document.querySelector("#people-list");
if (peopleList) {
    for (const data of Object.values(people)) {
        const link = document.createElement("a");
        link.href = data["website"];
        link.innerText = data["name"];
        const item = document.createElement("li");
        item.appendChild(link);
        peopleList.appendChild(item);
    }
}
