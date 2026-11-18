(function () {
  // The diagrams follow the page's light or dark mode, in the toolbox's design colours.
  var dark = document.querySelector('[rel="stylesheet"]').getAttribute("href").indexOf("dark") !== -1;
  var font = "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif";
  if (dark) {
    return {
      theme: "base",
      themeVariables: {
        darkMode: true,
        background: "#1e1e1e",
        primaryColor: "#1b2b3a",
        primaryBorderColor: "#4cc2ff",
        primaryTextColor: "#ffffff",
        lineColor: "#99d9ff",
        secondaryColor: "#2d2d2d",
        tertiaryColor: "#1e1e1e",
        clusterBkg: "#202a33",
        clusterBorder: "#4cc2ff",
        titleColor: "#ffffff",
        actorBkg: "#1b2b3a",
        actorBorder: "#4cc2ff",
        actorTextColor: "#ffffff",
        signalColor: "#99d9ff",
        signalTextColor: "#ffffff",
        noteBkgColor: "#2d2d2d",
        noteTextColor: "#ffffff",
        fontFamily: font
      }
    };
  }
  return {
    theme: "base",
    themeVariables: {
      primaryColor: "#e6f0fa",
      primaryBorderColor: "#0078d4",
      primaryTextColor: "#000000",
      lineColor: "#005a9e",
      secondaryColor: "#f3f3f3",
      tertiaryColor: "#ffffff",
      clusterBkg: "#f5f9fd",
      clusterBorder: "#0078d4",
      actorBkg: "#e6f0fa",
      actorBorder: "#0078d4",
      signalColor: "#005a9e",
      fontFamily: font
    }
  };
})()
