// Quick note: one-line comment added as requested.
function storePageContent() {
  const pageTitle = document.title;
  const pageContent = document.body.innerText;

  chrome.runtime.sendMessage({
    action: "storePageContent",
    data: { title: pageTitle, content: pageContent },
  });
}

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === "storePageContent") {
    storePageContent();
  }
});
