# Concept Document: Boolean Circuit Explorer & Generator

## 1. Overview
A single-file, dependency-free browser application for designing, exploring, generating, and evaluating boolean circuits. The application targets small-to-medium scale logic networks (up to ~10 inputs, ~5 outputs, ~20+ gates) containing **AND**, **XOR**, and **NOT** gates. It runs entirely offline from a single `index.html` file using Vanilla JavaScript and HTML5 Canvas.

## 2. Core Interaction & Editing
The user interface is split into a resizable sidebar for controls/code and a primary canvas for the interactive circuit diagram.

* **Node Manipulation:** Users can freely drag and drop nodes around the canvas.
* **Intuitive Wiring:** Gates have visual port circles. Users create connections by clicking and dragging a wire from an output port to an input port. 
* **Strict Structural Integrity:** The wiring logic prevents cyclic connections (loops) and strictly enforces gate limits:
    * **AND / XOR:** Maximum of 2 inputs.
    * **NOT / OUTPUT:** Maximum of 1 input.
* **Targeted Deletion:** Users can select individual nodes or specific wires (which become thicker and highlighted when clicked) and press the `Delete` or `Backspace` key to remove them.

## 3. Exploring & Visual Tracing
The application features a robust, color-coded tracing system to help users understand complex logic flows. 
* **Path Highlighting:** Selecting a node or a wire instantly highlights its entire connected data path:
    * **Yellow:** Dependencies (Ancestors / upstream data).
    * **Pink:** Influences (Descendants / downstream data).
    * **Orange/Peach:** Nodes that act as *both* a dependency for one selected item and an influence for another.
* **Multi-Select:** Users can `Shift + Click` to select multiple gates and wires simultaneously, allowing for complex path intersection analysis. 
* **Dynamic Contrast:** Node text automatically switches to dark colors when highlighted to maintain readability against the bright tracing colors.

## 4. Circuit Generation (DSL)
A resizable sidebar houses a built-in text editor exposing a Domain Specific Language (DSL) for programmatic circuit generation.
* **Commands:** Simple API using `create('TYPE')` (returns an ID) and `connect(fromId, toId)`.
* **Default Behavior:** The default script acts as a "Random Circuit Generator." It programmatically creates a random number of inputs, outputs, and intermediate gates, linking them together strictly to ensure a valid, feed-forward topology every time the script is executed.

## 5. Real-Time Evaluation
The circuit simulates logic continuously and visually represents the state of the data.
* **Interactive Inputs:** Users can double-click any `INPUT` node to toggle its boolean state between `0` (False) and `1` (True).
* **Live Wires:** Wires physically change color based on the data they carry (e.g., green for `1`, gray for `0`).
* **Node Values:** Each node actively displays its current computed output value on the canvas.

## 6. Layout Normalization
A dedicated **"Normalize Layout"** feature automatically organizes messy circuits into readable, left-to-right schemas.
* **Topological Sorting:** Calculates the longest path from any input to determine a node's "Level."
* **Grid Placement:** Inputs are locked to the far left (Level 0), and all subsequent gates are pushed into vertical columns based on their logical depth, ensuring signals flow cleanly from left to right.
* **Optimization:** Before laying out the grid, it scans for and automatically deletes redundant double-negations (e.g., a `NOT` gate feeding directly into another `NOT` gate), bypassing them to clean up the logic.

## 7. Intelligent Pruning & Healing
A **"Prune Circuit"** feature allows users to clean up generated or manually edited circuits to ensure mathematical relevance.
* **Dead-End Removal:** Automatically traces backward from all `OUTPUT` nodes and deletes any gates that do not ultimately contribute to an output.
* **Invalid Gate Healing:** If an `AND` or `XOR` gate is left with only 1 input, it is flagged as mathematically invalid. Instead of breaking the circuit, the algorithm "heals" it by deleting the invalid gate and stitching the incoming wire directly to the downstream target, preserving the overall data flow.

## 8. State Persistence (Import / Export)
Users can save their work directly to their local machine.
* **Export JSON:** Serializes the exact topological state (nodes, types, coordinates, and connections) into a lightweight `.json` file.
* **Import JSON:** Allows users to upload a previously saved circuit, instantly rebuilding the visual canvas and logic state.