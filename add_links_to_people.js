for ([id, data] of Object.entries(people)) {
    for (link of document.querySelectorAll("a." + id)) {
        link.href = data["website"]
    }
}