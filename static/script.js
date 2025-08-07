document.addEventListener('DOMContentLoaded', () => {
    const productListDiv = document.getElementById('product-list');
    const paymentSection = document.getElementById('payment-section');
    const qrCodeContainer = document.getElementById('qr-code-container');
    const paymentStatusMessage = document.getElementById('payment-status-message');
    const dispensingSection = document.getElementById('dispensing-section');
    const dispensingMessage = document.getElementById('dispensing-message');
    const addProductForm = document.getElementById('add-product-form');
    const addProductMessage = document.getElementById('add-product-message');
    const backToProductsButton = document.getElementById('back-to-products');

    let selectedProductId = null;

    // Función para mostrar/ocultar secciones
    function showSection(sectionId) {
        document.querySelectorAll('.container > div').forEach(section => {
            // Oculta todas las secciones principales
            if (section.id !== 'add-product-form' && section.id !== 'add-product-message' && section.id !== 'product-list') {
                 section.style.display = 'none';
            }
        });

        // Muestra las secciones relevantes para cada vista
        if (sectionId === 'product-list-view') {
            document.querySelector('.admin-section:nth-of-type(1)').style.display = 'block'; // Sección Añadir Producto
            document.querySelector('.admin-section:nth-of-type(2)').style.display = 'block'; // Sección Productos Existentes
        } else if (sectionId === 'payment-section') {
            document.querySelector('.admin-section:nth-of-type(1)').style.display = 'none';
            document.querySelector('.admin-section:nth-of-type(2)').style.display = 'none';
            paymentSection.style.display = 'block';
        } else if (sectionId === 'dispensing-section') {
            document.querySelector('.admin-section:nth-of-type(1)').style.display = 'none';
            document.querySelector('.admin-section:nth-of-type(2)').style.display = 'none';
            paymentSection.style.display = 'none';
            dispensingSection.style.display = 'block';
        }
    }

    // Función para cargar productos
    async function fetchProducts() {
        productListDiv.innerHTML = '<p>Cargando productos...</p>';
        try {
            const response = await fetch('/api/productos');
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            const products = await response.json();
            productListDiv.innerHTML = ''; // Limpiar mensaje de cargando

            if (products.length === 0) {
                productListDiv.innerHTML = '<p>No hay productos configurados.</p>';
                return;
            }

            products.forEach(product => {
                const productCard = document.createElement('div');
                productCard.className = 'product-card';
                productCard.innerHTML = `
                    <h3>${product.nombre}</h3>
                    <p>${product.cantidad_ml} ml</p>
                    <p class="price">$${product.precio.toFixed(2)}</p>
                `;
                productCard.addEventListener('click', () => selectProduct(product));
                productListDiv.appendChild(productCard);
            });
        } catch (error) {
            console.error('Error al cargar productos:', error);
            productListDiv.innerHTML = '<p class="message error">Error al cargar productos. Intenta de nuevo más tarde.</p>';
        }
    }

    // Función para seleccionar un producto y generar QR
    async function selectProduct(product) {
        selectedProductId = product.id;
        showSection('payment-section');
        paymentStatusMessage.textContent = 'Generando código QR...';
        paymentStatusMessage.className = 'message';
        qrCodeContainer.innerHTML = ''; // Limpiar QR anterior

        try {
            const response = await fetch(`/api/generar_qr/${selectedProductId}`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                }
            });

            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }

            const data = await response.json();

            if (data.status === 'success' && data.qr_data) {
                let qrSrc = '';
                // Usamos un servicio externo para generar el QR de la URL de Mercado Pago
                qrSrc = `https://api.qrserver.com/v1/create-qr-code/?size=200x200&data=${encodeURIComponent(data.qr_data)}`;
                
                qrCodeContainer.innerHTML = `<img src="${qrSrc}" alt="Código QR de Mercado Pago">`;
                paymentStatusMessage.textContent = '¡Escanea para pagar!';
                paymentStatusMessage.className = 'message success';

            } else {
                paymentStatusMessage.textContent = 'Error al generar QR: ' + (data.error || 'Desconocido');
                paymentStatusMessage.className = 'message error';
            }

        } catch (error) {
            console.error('Error al generar QR:', error);
            paymentStatusMessage.textContent = 'Error al generar el código QR. Intenta de nuevo.';
            paymentStatusMessage.className = 'message error';
        }
    }

    // Manejar el envío del formulario para añadir producto
    addProductForm.addEventListener('submit', async (event) => {
        event.preventDefault(); // Evitar recarga de la página

        const nombre = document.getElementById('product-name').value;
        const cantidad_ml = parseInt(document.getElementById('product-quantity').value);
        const precio = parseFloat(document.getElementById('product-price').value);

        addProductMessage.textContent = 'Añadiendo producto...';
        addProductMessage.className = 'message';

        try {
            const response = await fetch('/api/productos', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ nombre, cantidad_ml, precio })
            });

            const data = await response.json();

            if (response.ok) {
                addProductMessage.textContent = 'Producto añadido exitosamente!';
                addProductMessage.className = 'message success';
                addProductForm.reset(); // Limpiar el formulario
                fetchProducts(); // Recargar la lista de productos
            } else {
                throw new Error(data.error || 'Error desconocido al añadir producto');
            }
        } catch (error) {
            console.error('Error al añadir producto:', error);
            addProductMessage.textContent = 'Error al añadir producto: ' + error.message;
            addProductMessage.className = 'message error';
        }
    });

    // Botón para volver a la lista de productos
    backToProductsButton.addEventListener('click', () => {
        showSection('product-list-view');
        fetchProducts(); // Recargar productos por si se añadió uno nuevo
    });

    // Inicializar la carga de productos y mostrar la vista principal al cargar la página
    showSection('product-list-view');
    fetchProducts();
});