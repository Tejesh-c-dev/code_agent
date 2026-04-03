function init() {
    // Select the header, content, and footer elements by their IDs
    const header = document.getElementById("header");
    const content = document.getElementById("content");
    const footer = document.getElementById("footer");

    // Example of setting up an initial configuration for the header
    header.textContent = "Welcome to My Simple HTML Page";
    
    // Setting up initial content
    content.innerHTML = "<p>This is the main content area.</p>";
    
    // Footer setup
    footer.textContent = "Footer © 2023";

    // Here, we could add more event listeners or configurations as needed
}

// Initialize the page when the DOM is fully loaded
document.addEventListener("DOMContentLoaded", init);